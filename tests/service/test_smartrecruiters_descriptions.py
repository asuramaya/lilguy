"""The composition rule is the point: a real posting can have an EMPTY
jobDescription while additionalInformation carries the only text there
is, so taking one section would store '' and stop asking, permanently.
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
import smartrecruiters_descriptions as sr  # noqa: E402

PID = "smartrecruiters:Visa:744000133907678"


@pytest.fixture(autouse=True)
def _clean_db():
    db.init_schema()
    with db.cursor() as cur:
        cur.execute("TRUNCATE postings, sources RESTART IDENTITY CASCADE")
    yield


def _source():
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (company, ats, config, status) "
            "VALUES ('Visa', 'smartrecruiters', %s, 'active') "
            "ON CONFLICT (company, ats) DO UPDATE SET status = 'active' RETURNING id",
            (psycopg2.extras.Json({"token": "Visa"}),),
        )
        return cur.fetchone()["id"]


def _posting(pid=PID, description=None):
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_id, source_entry, company, title, location, url, ats,
                                   category, status, description, first_seen, last_seen)
            VALUES (%s, %s, 'Visa', 'Visa', 'Ops Intern', 'Austin, TX', 'https://x',
                    'smartrecruiters', 'Payments Technology', 'open', %s, now(), now())
            """,
            (pid, _source(), description),
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


def _sections(**bodies):
    return {"jobAd": {"sections": {k: {"text": v, "title": k} for k, v in bodies.items()}}}


def test_composes_every_section_that_has_content():
    _posting()
    payload = _sections(companyDescription="<p>About Visa</p>", jobDescription="<p>You will do things</p>")
    sr.fetch_missing_descriptions(limit=5, pace=0, session=_session(lambda u: FakeResp(200, payload)))
    text = _desc()
    assert "About Visa" in text and "You will do things" in text
    assert "<p>" not in text, "markup must not reach the display text"


def test_an_empty_job_description_does_not_discard_the_rest():
    # Confirmed live on a real Visa posting: jobDescription was empty and
    # additionalInformation held the only text.
    _posting()
    payload = _sections(jobDescription="", additionalInformation="<p>EEO information</p>")
    sr.fetch_missing_descriptions(limit=5, pace=0, session=_session(lambda u: FakeResp(200, payload)))
    assert "EEO information" in _desc()


def test_a_posting_with_no_text_anywhere_is_recorded_as_attempted():
    _posting()
    sr.fetch_missing_descriptions(limit=5, pace=0, session=_session(lambda u: FakeResp(200, _sections())))
    assert _desc() == "", "'' means asked and answered, so it stops being claimed"
    assert sr.fetch_missing_descriptions(
        limit=5, pace=0, session=_session(lambda u: FakeResp(200, {})))["attempted"] == 0


def test_the_url_is_built_from_the_id_and_the_source_token():
    _posting()
    seen = []
    sr.fetch_missing_descriptions(
        limit=5, pace=0, session=_session(lambda u: seen.append(u) or FakeResp(200, _sections())))
    assert seen == ["https://api.smartrecruiters.com/v1/companies/Visa/postings/744000133907678"]


def test_a_malformed_id_is_retired_rather_than_retried_forever():
    _posting(pid="smartrecruiters:Visa")   # no job id
    out = sr.fetch_missing_descriptions(limit=5, pace=0, session=_session(lambda u: FakeResp(200, {})))
    assert out["empty"] == 1
    assert _desc("smartrecruiters:Visa") == ""


def test_a_transient_failure_backs_off_instead_of_blocking_the_queue():
    # The lesson from the Workday deadlock, built in from the start.
    _posting()
    out = sr.fetch_missing_descriptions(limit=5, pace=0, session=_session(lambda u: FakeResp(503)))
    assert out["deferred"] == 1
    assert _desc() is None
    assert sr.fetch_missing_descriptions(
        limit=5, pace=0, session=_session(lambda u: FakeResp(200, _sections(jobDescription="<p>x</p>")))
    )["attempted"] == 0, "a deferred row must not be instantly re-claimable"


def test_only_smartrecruiters_postings_are_claimed():
    _posting()
    with db.cursor() as cur:
        cur.execute("UPDATE postings SET ats = 'workday' WHERE id = %s", (PID,))
    assert sr.fetch_missing_descriptions(
        limit=5, pace=0, session=_session(lambda u: FakeResp(200, {})))["attempted"] == 0
