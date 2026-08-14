"""Proves the reconciliation claim by construction: export a live
Postgres postings table, then run it through the REAL batch-pipeline
machinery (build_feed.py's own build_and_write(), the same function
scrape.py itself calls) with zero changes to that code. If this passes,
the two pipelines are format-compatible, not just "should be" by
inspection.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ, reason="needs a scratch Postgres via DATABASE_URL"
)

import db  # noqa: E402
from export_to_batch_store import fetch_open_postings_as_dicts  # noqa: E402

from build_feed import build_and_write  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    db.init_schema()
    with db.cursor() as cur:
        cur.execute("TRUNCATE postings, scrape_runs, sources, discovery_candidates RESTART IDENTITY CASCADE")
    yield


def _insert(id_, company, title, location, status, ats="greenhouse"):
    now = datetime.now(timezone.utc)
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_entry, company, title, location, url, ats, category,
                                   description_snippet, status, first_seen, last_seen)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'Logistics & Transportation', '', %s, %s, %s)
            """,
            (id_, company, company, title, location, f"https://x/{id_}", ats, status, now, now),
        )


def test_export_shape_matches_posting_to_dict_plus_first_seen():
    _insert("gh:1", "Acme Corp", "Supply Chain Intern", "Austin, TX", "open")
    dicts = fetch_open_postings_as_dicts()
    assert len(dicts) == 1
    d = dicts[0]
    assert set(d.keys()) == {
        "id", "company", "title", "location", "url", "source", "category",
        "posted_at", "description_snippet", "source_entry", "first_seen",
    }
    assert d["source"] == "greenhouse"  # DB column `ats` maps to JSON key `source`
    assert isinstance(d["first_seen"], str)  # ISO string, not a datetime object


def test_duplicate_and_closed_postings_are_excluded():
    _insert("gh:1", "Acme Corp", "Supply Chain Intern", "Austin, TX", "open")
    _insert("muse:1", "Acme Corp", "Supply Chain Intern", "Austin, TX", "duplicate")
    _insert("gh:2", "Acme Corp", "Old Intern Role", "Austin, TX", "closed")

    dicts = fetch_open_postings_as_dicts()
    assert [d["id"] for d in dicts] == ["gh:1"]


def test_exported_file_round_trips_through_the_real_batch_pipeline():
    # Flexport is a trusted_company in this fork's own filters.yaml --
    # its posting should pass the filter with no keyword match needed,
    # proving the exported file's fields line up with what user_filter.py
    # actually reads (company, title, description_snippet, first_seen).
    _insert("gh:1", "Flexport", "Operations Intern", "San Francisco, CA", "open")
    _insert("gh:2", "Unrelated Startup", "Marketing Intern", "New York, NY", "open")

    with tempfile.TemporaryDirectory() as tmp:
        postings_file = Path(tmp) / "all_postings.json"
        out_file = Path(tmp) / "FEED.md"
        postings_file.write_text(json.dumps(fetch_open_postings_as_dicts(), indent=2))

        root = Path(__file__).parent.parent.parent
        count = build_and_write(postings_file, root / "filters.yaml", out_file, "test")

        assert count == 1  # Flexport (trusted company) passes; the unrelated one doesn't
        rendered = out_file.read_text()
        assert "Flexport" in rendered
        assert "Unrelated Startup" not in rendered
