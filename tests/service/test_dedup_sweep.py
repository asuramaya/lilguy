"""Integration tests for dedup.py's run_dedup_sweep() against a real
Postgres -- this is SQL correctness under test (a window-function rank
+ conditional UPDATE), the same reasoning as test_scheduler_reconciliation.py
for why this needs a real database rather than a mock.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ, reason="needs a scratch Postgres via DATABASE_URL"
)

import db  # noqa: E402
from dedup import compute_dedup_key, run_dedup_sweep  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    db.init_schema()
    with db.cursor() as cur:
        cur.execute("TRUNCATE postings, scrape_runs, sources, discovery_candidates RESTART IDENTITY CASCADE")
    yield


def _insert_posting(id_, company, title, location, ats, source_entry, first_seen, status="open"):
    dedup_key = compute_dedup_key(company, title, location)
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_entry, company, title, location, url, ats, category,
                                   description_snippet, status, dedup_key, first_seen, last_seen)
            VALUES (%s, %s, %s, %s, %s, %s, %s, '', '', %s, %s, %s, %s)
            """,
            (id_, source_entry, company, title, location, f"https://x/{id_}", ats, status,
             dedup_key, first_seen, first_seen),
        )


def _status(id_):
    with db.cursor() as cur:
        cur.execute("SELECT status, duplicate_of FROM postings WHERE id = %s", (id_,))
        return cur.fetchone()


def test_direct_source_outranks_aggregator():
    now = datetime.now(timezone.utc)
    _insert_posting("muse:1", "Acme Corp", "Supply Chain Intern", "Austin, TX", "muse", "The Muse", now)
    _insert_posting("gh:1", "Acme Corp", "Supply Chain Intern", "Austin, TX", "greenhouse", "Acme", now)

    with db.cursor() as cur:
        changed = run_dedup_sweep(cur)

    assert changed == 1  # only the loser needs to flip
    assert _status("gh:1") == {"status": "open", "duplicate_of": None}
    assert _status("muse:1") == {"status": "duplicate", "duplicate_of": "gh:1"}


def test_same_precedence_tier_ties_broken_by_first_seen():
    now = datetime.now(timezone.utc)
    earlier = now - timedelta(days=1)
    _insert_posting("muse:1", "Acme Corp", "Supply Chain Intern", "Austin, TX", "muse", "The Muse", earlier)
    _insert_posting("adzuna:1", "Acme Corp", "Supply Chain Intern", "Austin, TX", "adzuna", "Adzuna", now)

    with db.cursor() as cur:
        run_dedup_sweep(cur)

    assert _status("muse:1")["status"] == "open"       # seen first
    assert _status("adzuna:1")["status"] == "duplicate"


def test_no_collision_leaves_both_open():
    now = datetime.now(timezone.utc)
    _insert_posting("gh:1", "Acme Corp", "Supply Chain Intern", "Austin, TX", "greenhouse", "Acme", now)
    _insert_posting("gh:2", "Acme Corp", "Logistics Intern", "Dallas, TX", "greenhouse", "Acme", now)

    with db.cursor() as cur:
        changed = run_dedup_sweep(cur)

    assert changed == 0
    assert _status("gh:1")["status"] == "open"
    assert _status("gh:2")["status"] == "open"


def test_canonical_closing_promotes_duplicate_back_to_open():
    now = datetime.now(timezone.utc)
    _insert_posting("muse:1", "Acme Corp", "Supply Chain Intern", "Austin, TX", "muse", "The Muse", now)
    _insert_posting("gh:1", "Acme Corp", "Supply Chain Intern", "Austin, TX", "greenhouse", "Acme", now)
    with db.cursor() as cur:
        run_dedup_sweep(cur)
    assert _status("muse:1")["status"] == "duplicate"

    # The direct source's posting closes (e.g. the role filled and
    # scheduler.py's normal close-on-absence logic marked it closed).
    with db.cursor() as cur:
        cur.execute("UPDATE postings SET status = 'closed' WHERE id = 'gh:1'")
        changed = run_dedup_sweep(cur)

    assert changed == 1
    assert _status("muse:1") == {"status": "open", "duplicate_of": None}
    assert _status("gh:1")["status"] == "closed"  # untouched -- dedup never revives a closed posting


def test_closed_postings_are_never_swept():
    now = datetime.now(timezone.utc)
    _insert_posting("gh:1", "Acme Corp", "Supply Chain Intern", "Austin, TX", "greenhouse", "Acme", now,
                     status="closed")
    _insert_posting("muse:1", "Acme Corp", "Supply Chain Intern", "Austin, TX", "muse", "The Muse", now,
                     status="closed")

    with db.cursor() as cur:
        changed = run_dedup_sweep(cur)

    assert changed == 0
    assert _status("gh:1")["status"] == "closed"
    assert _status("muse:1")["status"] == "closed"


def test_postings_without_a_dedup_key_are_ignored():
    now = datetime.now(timezone.utc)
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_entry, company, title, location, url, ats, category,
                                   description_snippet, status, dedup_key, first_seen, last_seen)
            VALUES ('x:1', 'X', 'X', 'X', '', 'https://x/1', 'muse', '', '', 'open', NULL, %s, %s)
            """,
            (now, now),
        )
        changed = run_dedup_sweep(cur)
    assert changed == 0
    assert _status("x:1")["status"] == "open"


def test_sweep_is_idempotent():
    now = datetime.now(timezone.utc)
    _insert_posting("muse:1", "Acme Corp", "Supply Chain Intern", "Austin, TX", "muse", "The Muse", now)
    _insert_posting("gh:1", "Acme Corp", "Supply Chain Intern", "Austin, TX", "greenhouse", "Acme", now)

    with db.cursor() as cur:
        first = run_dedup_sweep(cur)
    with db.cursor() as cur:
        second = run_dedup_sweep(cur)

    assert first == 1
    assert second == 0  # nothing left to change on a re-run
