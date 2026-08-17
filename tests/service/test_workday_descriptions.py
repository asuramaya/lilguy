"""The three-state description column is what keeps this from becoming a
permanent background load against Workday, so most of these tests are
about which failures retry and which don't.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import psycopg2.extras
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ, reason="needs a scratch Postgres via DATABASE_URL"
)

import db  # noqa: E402
import workday_descriptions as wd  # noqa: E402

PATH = "/job/Somewhere/Ops-Intern_JR1"
PID = f"workday:acme:careers:{PATH}"


@pytest.fixture(autouse=True)
def _clean_db():
    db.init_schema()
    with db.cursor() as cur:
        cur.execute("TRUNCATE postings, sources RESTART IDENTITY CASCADE")
    yield


def _source():
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (company, ats, config, status) VALUES ('Acme', 'workday', %s, 'active') RETURNING id",
            (psycopg2.extras.Json({"tenant": "acme", "wd_host": "wd5", "site": "careers"}),),
        )
        return cur.fetchone()["id"]


def _posting(source_id, pid=PID, description=None):
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_id, source_entry, company, title, location, url, ats,
                                   category, status, description, first_seen, last_seen)
            VALUES (%s, %s, 'Acme', 'Acme', 'Ops Intern', 'Remote', 'https://x', 'workday',
                    'Logistics', 'open', %s, now(), now())
            """,
            (pid, source_id, description),
        )


def _desc(pid=PID):
    with db.cursor() as cur:
        cur.execute("SELECT description FROM postings WHERE id = %s", (pid,))
        return cur.fetchone()["description"]


class FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _session(handler):
    return SimpleNamespace(get=lambda url, **kw: handler(url))


def test_external_path_is_recovered_from_the_posting_id():
    # Ids are f"workday:{tenant}:{site}:{external_path}" and the path
    # contains slashes but never colons.
    assert wd._external_path(PID) == PATH


def test_a_successful_fetch_stores_readable_text():
    sid = _source()
    _posting(sid)
    html = "<p>About the role</p><ul><li>Do things</li></ul>"
    out = wd.fetch_missing_descriptions(
        limit=5, pace=0,
        session=_session(lambda url: FakeResp(200, {"jobPostingInfo": {"jobDescription": html}})))
    assert out["filled"] == 1
    stored = _desc()
    assert "About the role" in stored
    assert "• Do things" in stored     # structure kept, not flattened


def test_the_detail_url_is_the_cxs_endpoint():
    sid = _source()
    _posting(sid)
    seen = {}

    def handler(url):
        seen["url"] = url
        return FakeResp(200, {"jobPostingInfo": {"jobDescription": "x"}})

    wd.fetch_missing_descriptions(limit=5, pace=0, session=_session(handler))
    assert seen["url"] == f"https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/careers{PATH}"


def test_already_fetched_postings_are_never_refetched():
    # The whole cost argument depends on this: once is once.
    sid = _source()
    _posting(sid, description="already have it")
    calls = []
    out = wd.fetch_missing_descriptions(
        limit=5, pace=0, session=_session(lambda url: calls.append(url) or FakeResp(200, {})))
    assert out["attempted"] == 0
    assert calls == []
    assert _desc() == "already have it"


def test_empty_string_also_counts_as_already_attempted():
    sid = _source()
    _posting(sid, description="")
    assert wd.fetch_missing_descriptions(limit=5, pace=0,
                                          session=_session(lambda url: FakeResp(200, {})))["attempted"] == 0


def test_a_404_is_recorded_so_it_stops_being_requested():
    sid = _source()
    _posting(sid)
    out = wd.fetch_missing_descriptions(limit=5, pace=0, session=_session(lambda url: FakeResp(404)))
    assert out["empty"] == 1
    assert _desc() == ""          # attempted, definitively nothing there
    # ...and a second pass must not ask again.
    assert wd.fetch_missing_descriptions(limit=5, pace=0,
                                          session=_session(lambda url: FakeResp(404)))["attempted"] == 0


def test_a_transient_failure_stays_null_so_it_retries():
    sid = _source()
    _posting(sid)
    out = wd.fetch_missing_descriptions(limit=5, pace=0, session=_session(lambda url: FakeResp(503)))
    assert out["deferred"] == 1
    assert _desc() is None        # still eligible
    # Next pass picks it up again and can succeed.
    out2 = wd.fetch_missing_descriptions(
        limit=5, pace=0,
        session=_session(lambda url: FakeResp(200, {"jobPostingInfo": {"jobDescription": "<p>Now</p>"}})))
    assert out2["filled"] == 1


def test_an_exception_defers_rather_than_killing_the_batch():
    sid = _source()
    _posting(sid, pid=f"workday:acme:careers:{PATH}")
    _posting(sid, pid=f"workday:acme:careers:/job/Other/Intern_JR2")

    def handler(url):
        if "JR1" in url:
            raise ConnectionError("boom")
        return FakeResp(200, {"jobPostingInfo": {"jobDescription": "<p>Fine</p>"}})

    out = wd.fetch_missing_descriptions(limit=5, pace=0, session=_session(handler))
    assert out["deferred"] == 1
    assert out["filled"] == 1     # the other posting still got done


def test_a_posting_whose_provider_has_no_text_is_marked_attempted():
    sid = _source()
    _posting(sid)
    out = wd.fetch_missing_descriptions(
        limit=5, pace=0, session=_session(lambda url: FakeResp(200, {"jobPostingInfo": {}})))
    assert out["empty"] == 1
    assert _desc() == ""


def test_batch_is_bounded_by_limit():
    sid = _source()
    for i in range(5):
        _posting(sid, pid=f"workday:acme:careers:/job/X/Intern_JR{i}")
    out = wd.fetch_missing_descriptions(
        limit=2, pace=0,
        session=_session(lambda url: FakeResp(200, {"jobPostingInfo": {"jobDescription": "<p>x</p>"}})))
    assert out["attempted"] == 2
