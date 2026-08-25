"""backfill_standardize.py exists to clean postings stored before
scheduler.py's live ingest path was wired to service/standardize.py --
see that commit's message. clean_display_title/standardize_location's
own per-case logic is covered by test_standardize.py already; these
tests cover only what's unique to the backfill script itself: which
rows it touches, that --dry-run writes nothing, and that dedup_key is
never recomputed.
"""
import os
import sys
from pathlib import Path

import psycopg2.extras
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ, reason="needs a scratch Postgres via DATABASE_URL"
)

import db  # noqa: E402
import backfill_standardize  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    db.init_schema()
    with db.cursor() as cur:
        cur.execute("TRUNCATE postings, sources RESTART IDENTITY CASCADE")
    yield


def _source(company, ats="greenhouse"):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (company, ats, config, status) VALUES (%s, %s, %s, 'active') RETURNING id",
            (company, ats, psycopg2.extras.Json({})),
        )
        return cur.fetchone()["id"]


def _posting(pid, title, location, status="open", dedup_key="original-key"):
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_id, source_entry, company, title, location, url, ats,
                                   category, status, dedup_key, first_seen, last_seen)
            VALUES (%s, %s, 'Acme', 'Acme', %s, %s, %s, 'greenhouse', 'Logistics', %s, %s, now(), now())
            """,
            (pid, _source("Acme"), title, location, f"https://x/{pid}", status, dedup_key),
        )


def _row(pid):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM postings WHERE id = %s", (pid,))
        return cur.fetchone()


def test_backfill_cleans_a_messy_title_and_location():
    _posting("messy", "Supply Chain Intern - JR12345", "CA-QC-LONGUEUIL-J01 ~ 1000 Rue Test")
    result = backfill_standardize.run_backfill()
    assert result["changed_title"] == 1
    assert result["changed_location"] == 1
    row = _row("messy")
    assert row["title"] == "Supply Chain Intern"
    assert row["location"] == "Longueuil, QC"


def test_backfill_skips_already_clean_rows():
    _posting("clean", "Supply Chain Intern", "Longueuil, QC")
    result = backfill_standardize.run_backfill()
    assert result["unchanged"] == 1
    assert result["changed_title"] == 0
    assert result["changed_location"] == 0


def test_backfill_never_touches_closed_postings():
    _posting("dead", "Supply Chain Intern - JR12345", "CA-QC-LONGUEUIL-J01 ~ 1000 Rue Test", status="closed")
    result = backfill_standardize.run_backfill()
    assert result["total"] == 0
    row = _row("dead")
    assert row["title"] == "Supply Chain Intern - JR12345", "closed postings are never displayed -- cleaning buys nothing"


def test_dry_run_reports_but_writes_nothing():
    _posting("messy", "Supply Chain Intern - JR12345", "CA-QC-LONGUEUIL-J01 ~ 1000 Rue Test")
    result = backfill_standardize.run_backfill(dry_run=True)
    assert result["changed_title"] == 1
    row = _row("messy")
    assert row["title"] == "Supply Chain Intern - JR12345", "dry run must not write"


def test_backfill_never_recomputes_dedup_key():
    # dedup_key was computed from this row's RAW title/location at insert
    # time, same as every other row in the corpus -- recomputing it here
    # from the newly-cleaned values would silently stop matching this row
    # against the rest of the corpus.
    _posting("messy", "Supply Chain Intern - JR12345", "CA-QC-LONGUEUIL-J01 ~ 1000 Rue Test", dedup_key="original-key")
    backfill_standardize.run_backfill()
    assert _row("messy")["dedup_key"] == "original-key"
