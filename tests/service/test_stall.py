"""The failure mode here is a scheduler that LOOKS healthy: running,
logging, passing its health check, and achieving nothing.
"""
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ, reason="needs a scratch Postgres via DATABASE_URL"
)

import db  # noqa: E402
import psycopg2.extras  # noqa: E402
from stall import check_for_stall  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    db.init_schema()
    with db.cursor() as cur:
        cur.execute("TRUNCATE postings, sources, scrape_runs, events RESTART IDENTITY CASCADE")
    yield


def _source():
    """One row, reused -- (company, ats) is unique, so a fresh insert per
    run violates the constraint on the second run of any test."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (company, ats, config, status) "
            "VALUES ('Acme', 'greenhouse', %s, 'active') "
            "ON CONFLICT (company, ats) DO UPDATE SET status = 'active' RETURNING id",
            (psycopg2.extras.Json({}),),
        )
        return cur.fetchone()["id"]


def _run(hours_ago, ok=True):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO scrape_runs (source_id, started_at, ok, fetched_count, internship_count) "
            "VALUES (%s, now() - make_interval(hours => %s), %s, 10, 1)",
            (_source(), hours_ago, ok),
        )


def _events():
    with db.cursor() as cur:
        cur.execute("SELECT kind, detail FROM events WHERE kind = 'stalled' ORDER BY id")
        return cur.fetchall()


def test_a_recent_success_is_not_a_stall():
    _run(hours_ago=1)
    with db.cursor() as cur:
        assert check_for_stall(cur)["stalled"] is False
    assert _events() == []


def test_no_successful_run_in_hours_is_a_stall():
    _run(hours_ago=9)
    with db.cursor() as cur:
        out = check_for_stall(cur)
    assert out["stalled"] is True and out["emitted"] is True
    assert "9" in _events()[0]["detail"]


def test_activity_without_success_still_counts_as_stalled():
    # The distinction the whole module exists for: a scheduler failing
    # every source every cycle is maximally busy and achieving nothing.
    _run(hours_ago=9, ok=True)
    for h in (2, 1, 0):
        _run(hours_ago=h, ok=False)
    with db.cursor() as cur:
        assert check_for_stall(cur)["stalled"] is True


def test_a_stall_is_reported_once_not_every_cycle():
    # Repeating it each cycle turns the events panel into a stuck alarm
    # that nobody reads.
    _run(hours_ago=9)
    with db.cursor() as cur:
        assert check_for_stall(cur)["emitted"] is True
    for _ in range(5):
        with db.cursor() as cur:
            assert check_for_stall(cur)["emitted"] is False
    assert len(_events()) == 1


def test_a_new_stall_after_a_recovery_reports_again():
    # Each check gets its OWN cursor block so its write is committed
    # before the next statement reads. Nesting a read inside the writing
    # transaction would see nothing, which is a property of the test
    # harness rather than of the code under test.
    _run(hours_ago=9)
    with db.cursor() as cur:
        check_for_stall(cur)
    assert len(_events()) == 1

    _run(hours_ago=0)  # recovery: a success NEWER than the alert
    with db.cursor() as cur:
        assert check_for_stall(cur)["stalled"] is False

    # Simulate nine hours passing since that recovery by shifting the
    # whole history back, alert included. Ageing only the runs -- as this
    # test first did -- leaves the old alert NEWER than the recovery,
    # which is a state real time can never produce, and the code
    # correctly declined to re-report it.
    with db.cursor() as cur:
        cur.execute("UPDATE scrape_runs SET started_at = started_at - interval '9 hours'")
        cur.execute("UPDATE events SET created_at = created_at - interval '9 hours'")
    with db.cursor() as cur:
        assert check_for_stall(cur)["emitted"] is True
    assert len(_events()) == 2


def test_a_fresh_install_with_no_runs_is_not_a_fault():
    # Otherwise every new deployment greets its owner with a fault report.
    with db.cursor() as cur:
        out = check_for_stall(cur)
    assert out["stalled"] is False
    assert out["reason"] == "no successful run yet"
    assert _events() == []


def test_the_threshold_is_configurable_for_a_forker():
    _run(hours_ago=2)
    with db.cursor() as cur:
        assert check_for_stall(cur, stall_after=timedelta(hours=1))["stalled"] is True
        assert check_for_stall(cur, stall_after=timedelta(hours=6))["stalled"] is False
