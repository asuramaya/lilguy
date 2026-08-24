"""The asymmetry is the whole design: wrongly closing a LIVE posting
costs a reader a job they could have had, while leaving a dead one up
one cycle longer costs a wasted click. So only a definitive 404/410
closes anything; everything else is "we did not find out".
"""
import os
import sys
from datetime import datetime, timedelta, timezone
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
import liveness  # noqa: E402

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _clean_db():
    db.init_schema()
    with db.cursor() as cur:
        cur.execute("TRUNCATE postings, sources, events RESTART IDENTITY CASCADE")
    yield


def _source():
    """One row, reused. (company, ats) is unique, so calling this per
    posting -- as this file first did -- violates the constraint on the
    second posting of any test."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (company, ats, config, status) "
            "VALUES ('The Muse', 'muse', %s, 'active') "
            "ON CONFLICT (company, ats) DO UPDATE SET status = 'active' RETURNING id",
            (psycopg2.extras.Json({}),),
        )
        return cur.fetchone()["id"]


def _posting(pid, days_old=400, status="open", url=None):
    # `is None`, not `or`: url="" is a REAL case this file tests, and an
    # `or` default silently replaces it with a working URL.
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_id, source_entry, company, title, location, url, ats,
                                   category, status, posted_at_ts, first_seen, last_seen)
            VALUES (%s, %s, 'The Muse', 'Acme', 'Ops Intern', 'Remote', %s, 'muse',
                    'Logistics', %s, %s, now(), now())
            """,
            (pid, _source(), f"https://themuse.com/jobs/{pid}" if url is None else url, status,
             NOW - timedelta(days=days_old)),
        )


def _row(pid):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM postings WHERE id = %s", (pid,))
        return cur.fetchone()


def _workday_posting(pid, url):
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_id, source_entry, company, title, location, url, ats,
                                   category, status, posted_at_ts, first_seen, last_seen)
            VALUES (%s, %s, 'Acme Careers', 'Acme', 'Ops Intern', 'Remote', %s, 'workday',
                    'Logistics', 'open', %s, now(), now())
            """,
            (pid, _source(), url, NOW - timedelta(days=10)),
        )


class FakeResp:
    def __init__(self, status_code, history=None, url=""):
        self.status_code = status_code
        self.history = history or []
        self.url = url
        self.text = ""


def _session(handler):
    return SimpleNamespace(get=lambda url, **kw: handler(url))


def test_a_404_closes_the_posting():
    _posting("dead")
    out = liveness.run_liveness_sweep(limit=5, pace=0, session=_session(lambda u: FakeResp(404)))
    assert out["closed"] == 1
    row = _row("dead")
    assert row["status"] == "closed"
    assert row["closed_at"] is not None


def test_a_200_leaves_it_open_and_schedules_the_next_check():
    _posting("live")
    out = liveness.run_liveness_sweep(limit=5, pace=0, session=_session(lambda u: FakeResp(200)))
    assert out["alive"] == 1
    row = _row("live")
    assert row["status"] == "open"
    assert row["liveness_checked_at"] is not None
    assert row["liveness_next_check_at"] > NOW


@pytest.mark.parametrize("code", [403, 429, 500, 503])
def test_a_non_definitive_response_never_closes_a_posting(code):
    # The costly mistake is closing something a reader could still apply
    # to, so anything short of "gone" must leave it open.
    _posting("maybe")
    out = liveness.run_liveness_sweep(limit=5, pace=0, session=_session(lambda u: FakeResp(code)))
    assert out["closed"] == 0
    assert out["deferred"] == 1
    assert _row("maybe")["status"] == "open"


def test_a_network_error_never_closes_a_posting():
    _posting("maybe")

    def boom(url):
        raise OSError("connection reset")

    out = liveness.run_liveness_sweep(limit=5, pace=0, session=_session(boom))
    assert out["closed"] == 0
    assert _row("maybe")["status"] == "open"


def test_a_redirect_to_greenhouses_error_page_closes_the_posting():
    # Measured live: Greenhouse never 404s a dead job's own URL -- it
    # redirects (200, after following) to the board root with
    # "?error=true" appended, job id dropped from the path entirely.
    # Sampled 150 open Greenhouse postings this way: 18 (12%) were dead,
    # every one landing on error=true, zero false positives.
    _posting("dead-via-redirect", url="https://job-boards.greenhouse.io/acme/jobs/12345")
    resp = FakeResp(200, history=[FakeResp(301)], url="https://job-boards.greenhouse.io/acme?error=true")
    out = liveness.run_liveness_sweep(limit=5, pace=0, session=_session(lambda u: resp))
    assert out["closed"] == 1
    assert _row("dead-via-redirect")["status"] == "closed"


def test_a_harmless_redirect_that_keeps_the_job_id_stays_open():
    # The same sample had redirects that are NOT closures -- a
    # boards.greenhouse.io -> job-boards.greenhouse.io host migration, a
    # trailing-slash cleanup -- distinguished from a real closure only by
    # whether error=true shows up in the final URL, never by "a redirect
    # happened" alone.
    _posting("moved-not-dead", url="https://boards.greenhouse.io/acme/jobs/12345?gh_jid=12345")
    resp = FakeResp(200, history=[FakeResp(301)], url="https://job-boards.greenhouse.io/acme/jobs/12345?gh_jid=12345")
    out = liveness.run_liveness_sweep(limit=5, pace=0, session=_session(lambda u: resp))
    assert out["closed"] == 0
    assert out["alive"] == 1
    assert _row("moved-not-dead")["status"] == "open"


def test_workday_cxs_404_closes_the_posting():
    pid = "workday:acme:Acme_Careers:/job/some-role_R123"
    _workday_posting(pid, "https://acme.wd1.myworkdayjobs.com/Acme_Careers/job/some-role_R123")
    out = liveness.run_liveness_sweep(limit=5, pace=0, session=_session(lambda u: FakeResp(404)))
    assert out["closed"] == 1
    assert _row(pid)["status"] == "closed"


def test_workday_cxs_403_defers_rather_than_closes():
    # Measured live against a real tenant: Workday's per-posting CXS
    # endpoint 403s on postings the source's own list fetch still
    # confirms open -- at scale this closed and reopened the same
    # postings over 100k times in a week (CVS Health, Enterprise
    # Mobility, TikTok). A 403 here is "we got blocked", not "it's
    # gone" -- same rule as every other non-Workday source.
    pid = "workday:acme:Acme_Careers:/job/some-role_R123"
    _workday_posting(pid, "https://acme.wd1.myworkdayjobs.com/Acme_Careers/job/some-role_R123")
    out = liveness.run_liveness_sweep(limit=5, pace=0, session=_session(lambda u: FakeResp(403)))
    assert out["closed"] == 0
    assert out["deferred"] == 1
    assert _row(pid)["status"] == "open"


def test_oldest_postings_are_checked_first():
    # Age does not decide the answer, but it is the best predictor of
    # which postings are worth asking about.
    _posting("newest", days_old=1)
    _posting("oldest", days_old=900)
    _posting("middle", days_old=200)
    seen = []
    liveness.run_liveness_sweep(limit=2, pace=0,
                                session=_session(lambda u: seen.append(u) or FakeResp(200)))
    assert "oldest" in seen[0]
    assert "middle" in seen[1]


def test_a_deferred_posting_does_not_block_the_queue():
    # The description backfill deadlocked exactly this way: a row that
    # always failed stayed instantly re-claimable at the head of the
    # queue and starved everything behind it.
    _posting("blocker", days_old=900)
    _posting("victim", days_old=800)

    def handler(url):
        return FakeResp(500) if "blocker" in url else FakeResp(404)

    first = liveness.run_liveness_sweep(limit=1, pace=0, session=_session(handler))
    assert first["deferred"] == 1
    second = liveness.run_liveness_sweep(limit=1, pace=0, session=_session(handler))
    assert second["closed"] == 1, "the queue must advance past a posting that keeps failing"


def test_closed_postings_are_not_rechecked():
    _posting("already", status="closed")
    assert liveness.run_liveness_sweep(
        limit=5, pace=0, session=_session(lambda u: FakeResp(404)))["checked"] == 0


def test_a_live_posting_is_not_rechecked_immediately():
    _posting("live")
    liveness.run_liveness_sweep(limit=5, pace=0, session=_session(lambda u: FakeResp(200)))
    assert liveness.run_liveness_sweep(
        limit=5, pace=0, session=_session(lambda u: FakeResp(200)))["checked"] == 0


def test_old_but_live_postings_come_back_round_sooner_than_fresh_ones():
    _posting("ancient", days_old=900)
    _posting("recent", days_old=2)
    liveness.run_liveness_sweep(limit=5, pace=0, session=_session(lambda u: FakeResp(200)))
    assert _row("ancient")["liveness_next_check_at"] < _row("recent")["liveness_next_check_at"]


def test_closing_records_an_event_so_it_is_visible():
    _posting("dead")
    liveness.run_liveness_sweep(limit=5, pace=0, session=_session(lambda u: FakeResp(404)))
    with db.cursor() as cur:
        cur.execute("SELECT kind, company, detail FROM events WHERE kind = 'expired'")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert "404" in rows[0]["detail"]


def test_a_posting_with_no_url_is_skipped_rather_than_guessed_at():
    _posting("nourl", url="")
    assert liveness.run_liveness_sweep(
        limit=5, pace=0, session=_session(lambda u: FakeResp(404)))["checked"] == 0
    assert _row("nourl")["status"] == "open"


def test_undated_postings_do_not_jump_the_queue():
    # Caught live: with NULLS FIRST the sweep spent its first 36 slots on
    # undated postings before reaching a single aged one. No date is not
    # evidence of age, so it must not earn priority.
    _posting("old", days_old=900)
    with db.cursor() as cur:
        cur.execute("UPDATE postings SET posted_at_ts = NULL WHERE id = 'old'")
    _posting("older", days_old=800)
    seen = []
    liveness.run_liveness_sweep(limit=1, pace=0,
                                session=_session(lambda u: seen.append(u) or FakeResp(200)))
    assert "older" in seen[0], "a dated posting must be checked before an undated one"
