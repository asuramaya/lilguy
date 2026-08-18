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


def test_the_detail_url_is_rebuilt_from_the_posting_id_and_source_config():
    # Ids are f"workday:{tenant}:{site}:{external_path}" and the path
    # contains slashes, so the split must be bounded rather than greedy.
    # Asserted through build_url rather than a private helper: the URL is
    # the behaviour that matters, and it survives the internals moving
    # into description_backfill.py.
    row = {"id": PID, "tenant": "acme", "wd_host": "wd5", "site": "careers"}
    assert wd.WorkdayDescriptions().build_url(row) == (
        f"https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/careers{PATH}")


def test_a_row_whose_source_config_is_incomplete_is_retired_not_retried():
    # No tenant means no URL can ever be built, so retrying is pointless.
    row = {"id": PID, "tenant": None, "wd_host": "wd5", "site": "careers"}
    assert wd.WorkdayDescriptions().build_url(row) is None


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


def test_a_transient_failure_stays_null_and_retries_after_its_backoff():
    sid = _source()
    _posting(sid)
    out = wd.fetch_missing_descriptions(limit=5, pace=0, session=_session(lambda url: FakeResp(503)))
    assert out["deferred"] == 1
    assert _desc() is None        # still eligible in principle...

    # ...but NOT immediately. Being instantly re-claimable is precisely
    # what deadlocked this queue in production.
    assert wd.fetch_missing_descriptions(
        limit=5, pace=0,
        session=_session(lambda url: FakeResp(200, {"jobPostingInfo": {"jobDescription": "<p>Now</p>"}}))
    )["attempted"] == 0

    _rewind()
    out2 = wd.fetch_missing_descriptions(
        limit=5, pace=0,
        session=_session(lambda url: FakeResp(200, {"jobPostingInfo": {"jobDescription": "<p>Now</p>"}})))
    assert out2["filled"] == 1


def _rewind(pid=PID):
    """Pretend the backoff window has elapsed."""
    with db.cursor() as cur:
        cur.execute(
            "UPDATE postings SET description_next_attempt_at = now() - interval '1 minute' WHERE id = %s",
            (pid,),
        )


def _retry_state(pid=PID):
    with db.cursor() as cur:
        cur.execute(
            "SELECT description_attempts, description_next_attempt_at FROM postings WHERE id = %s",
            (pid,),
        )
        return cur.fetchone()


def test_a_permanently_failing_row_cannot_starve_the_ones_behind_it():
    """The exact production deadlock, reproduced.

    The claim query had no attempt tracking, so a row that always failed
    stayed instantly re-claimable and sat at the head of the queue
    forever. Live symptom: "0 filled, 0 none-available, 10 deferred"
    every cycle, with 465 postings behind it that were never once tried.
    """
    sid = _source()
    # The victim goes in FIRST so the stuck row has the later first_seen
    # and genuinely sits at the HEAD of the queue (claim order is
    # first_seen DESC). Otherwise this test would pass by luck rather
    # than by fix.
    _posting(sid)  # the victim, queued behind it
    stuck = "workday:acme:careers:/job/Nowhere/Stuck_JR0"
    _posting(sid, pid=stuck)

    def handler(url):
        return FakeResp(503) if "Stuck" in url else FakeResp(
            200, {"jobPostingInfo": {"jobDescription": "<p>Real</p>"}})

    first = wd.fetch_missing_descriptions(limit=1, pace=0, session=_session(handler))
    assert first["deferred"] == 1

    # Before the fix this claimed the SAME stuck row again and the other
    # posting was never reached.
    second = wd.fetch_missing_descriptions(limit=1, pace=0, session=_session(handler))
    assert second["filled"] == 1, "the queue must advance past a row that keeps failing"
    assert _desc() == "Real"


def test_403_is_terminal_because_workday_uses_it_for_unpublished_jobs():
    """Verified against the live platform, not assumed: a stored path
    returns 403 (errorCode S22) while a freshly-listed path from the same
    tenant returns 200 in the same second. Classifying it as transient is
    what made dead rows retry forever."""
    sid = _source()
    _posting(sid)
    out = wd.fetch_missing_descriptions(limit=5, pace=0, session=_session(lambda url: FakeResp(403)))
    assert out["empty"] == 1
    assert out["deferred"] == 0
    assert _desc() == "", "403 means gone -- record 'attempted, none available' and stop asking"
    assert wd.fetch_missing_descriptions(
        limit=5, pace=0, session=_session(lambda url: FakeResp(403)))["attempted"] == 0


def test_backoff_lengthens_with_repeated_failure_and_is_capped():
    sid = _source()
    _posting(sid)
    seen = []
    for _ in range(len(wd.BACKOFF_HOURS) + 2):
        wd.fetch_missing_descriptions(limit=5, pace=0, session=_session(lambda url: FakeResp(503)))
        seen.append(_retry_state()["description_attempts"])
        _rewind()
    assert seen == sorted(seen), "attempts must climb monotonically"
    assert seen[-1] == len(wd.BACKOFF_HOURS) + 2

    # Capped rather than unbounded: a tenant down for a week must still
    # come back around, just never often enough to monopolise the queue.
    with db.cursor() as cur:
        cur.execute(
            "SELECT description_next_attempt_at - now() AS gap FROM postings WHERE id = %s", (PID,))
        gap_hours = cur.fetchone()["gap"].total_seconds() / 3600
    assert gap_hours <= max(wd.BACKOFF_HOURS) + 1


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
