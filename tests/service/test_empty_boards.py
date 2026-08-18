"""An empty BOARD and an empty RESULT are different claims.

A board returning 200 with zero jobs will never yield anything and is
scraped forever at full cadence, because every other self-healing path
in this project keys on failure. A board returning 200 jobs of which
none are internships is perfectly healthy and must not be touched.
"""
import os
import sys
from pathlib import Path

import psycopg2.extras
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ, reason="needs a scratch Postgres via DATABASE_URL"
)

import db  # noqa: E402
from empty_boards import EMPTY_RUNS_BEFORE_BACKOFF, run_empty_board_sweep  # noqa: E402

NORMAL_INTERVAL = 1800


@pytest.fixture(autouse=True)
def _clean_db():
    db.init_schema()
    with db.cursor() as cur:
        cur.execute("TRUNCATE postings, sources, scrape_runs RESTART IDENTITY CASCADE")
    yield


def _source(company, status="active", interval=NORMAL_INTERVAL):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (company, ats, config, status, scrape_interval_seconds) "
            "VALUES (%s, 'greenhouse', %s, %s, %s) RETURNING id",
            (company, psycopg2.extras.Json({}), status, interval),
        )
        return cur.fetchone()["id"]


def _runs(source_id, count, fetched, ok=True):
    with db.cursor() as cur:
        for i in range(count):
            cur.execute(
                "INSERT INTO scrape_runs (source_id, started_at, ok, fetched_count, internship_count) "
                "VALUES (%s, now() - make_interval(hours => %s), %s, %s, 0)",
                (source_id, count - i, ok, fetched),
            )


def _interval(company):
    with db.cursor() as cur:
        cur.execute("SELECT scrape_interval_seconds FROM sources WHERE company = %s", (company,))
        return cur.fetchone()["scrape_interval_seconds"]


def test_a_board_that_returns_nothing_at_all_is_backed_off():
    # Koch Industries: a valid {"jobs":[],"meta":{"total":0}} across all
    # 37 runs since it was added.
    sid = _source("Koch")
    _runs(sid, EMPTY_RUNS_BEFORE_BACKOFF, fetched=0)
    with db.cursor() as cur:
        assert run_empty_board_sweep(cur) == 1
    assert _interval("Koch") > NORMAL_INTERVAL


def test_a_healthy_board_with_no_internships_keeps_its_cadence():
    # THE distinction. Nine real sources had zero open postings while
    # fetching 78-221 jobs per run -- they simply have no internships
    # right now, which is ordinary.
    sid = _source("Flexport")
    _runs(sid, EMPTY_RUNS_BEFORE_BACKOFF, fetched=154)
    with db.cursor() as cur:
        assert run_empty_board_sweep(cur) == 0
    assert _interval("Flexport") == NORMAL_INTERVAL


def test_a_single_failure_in_the_window_blocks_the_backoff():
    # A failed run means we do not KNOW the board is empty, only that we
    # could not read it -- which is the failure path's business.
    sid = _source("Flaky")
    _runs(sid, EMPTY_RUNS_BEFORE_BACKOFF - 1, fetched=0)
    _runs(sid, 1, fetched=0, ok=False)
    with db.cursor() as cur:
        assert run_empty_board_sweep(cur) == 0
    assert _interval("Flaky") == NORMAL_INTERVAL


def test_too_few_runs_to_judge_yet():
    sid = _source("New")
    _runs(sid, EMPTY_RUNS_BEFORE_BACKOFF - 1, fetched=0)
    with db.cursor() as cur:
        assert run_empty_board_sweep(cur) == 0


def test_only_the_recent_window_counts_so_a_board_can_recover():
    # A board that was empty for months and is now posting must not be
    # backed off on the strength of its history.
    sid = _source("Revived")
    _runs(sid, 40, fetched=0)
    _runs(sid, EMPTY_RUNS_BEFORE_BACKOFF, fetched=25)
    with db.cursor() as cur:
        assert run_empty_board_sweep(cur) == 0
    assert _interval("Revived") == NORMAL_INTERVAL


def test_the_sweep_is_idempotent():
    sid = _source("Koch")
    _runs(sid, EMPTY_RUNS_BEFORE_BACKOFF, fetched=0)
    with db.cursor() as cur:
        assert run_empty_board_sweep(cur) == 1
        assert run_empty_board_sweep(cur) == 0, "an already-backed-off source must not be touched again"


def test_disabled_sources_are_left_to_their_own_path():
    sid = _source("Broken", status="disabled")
    _runs(sid, EMPTY_RUNS_BEFORE_BACKOFF, fetched=0)
    with db.cursor() as cur:
        assert run_empty_board_sweep(cur) == 0
