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


class FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


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
