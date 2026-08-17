"""Proves the self-healing sweep against a real Postgres -- same
DATABASE_URL convention as test_scheduler_reconciliation.py, skipped
automatically without it.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ, reason="needs a scratch Postgres via DATABASE_URL"
)

import db  # noqa: E402
import psycopg2.extras  # noqa: E402
from source_sync import run_source_sync_sweep  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    db.init_schema()
    with db.cursor() as cur:
        cur.execute("TRUNCATE postings, sources RESTART IDENTITY CASCADE")
    yield


def _insert_source(company, category):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (company, ats, category, config, status) "
            "VALUES (%s, 'greenhouse', %s, %s, 'active') RETURNING id",
            (company, category, psycopg2.extras.Json({})),
        )
        return cur.fetchone()["id"]


def _insert_posting(id_, source_id, company, category, status="open"):
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_id, source_entry, company, title, location, url,
                                   ats, category, status, dedup_key, first_seen, last_seen)
            VALUES (%s, %s, 'x', %s, 'Intern', 'Remote', 'https://x', 'greenhouse', %s, %s, %s, now(), now())
            """,
            (id_, source_id, company, category, status, f"dk:{id_}"),
        )


def test_sweep_resyncs_drifted_company_and_category():
    source_id = _insert_source("Acme Corporation", "Logistics")
    _insert_posting("p1", source_id, "acmecorp", "Uncategorized")

    with db.cursor() as cur:
        changed = run_source_sync_sweep(cur)
    assert changed == 1

    with db.cursor() as cur:
        cur.execute("SELECT company, category FROM postings WHERE id = 'p1'")
        r = cur.fetchone()
    assert r["company"] == "Acme Corporation"
    assert r["category"] == "Logistics"


def test_sweep_touches_closed_and_duplicate_postings_too():
    # The scheduler upsert only ever re-fetches OPEN postings from a
    # source's live board -- a closed or duplicate posting never comes
    # back through that path again, so it would stay stale forever
    # without this sweep reaching it independently of status.
    source_id = _insert_source("Acme Corporation", "Logistics")
    _insert_posting("p-closed", source_id, "acmecorp", "Uncategorized", status="closed")
    _insert_posting("p-dup", source_id, "acmecorp", "Uncategorized", status="duplicate")

    with db.cursor() as cur:
        changed = run_source_sync_sweep(cur)
    assert changed == 2

    with db.cursor() as cur:
        cur.execute("SELECT id, company, category FROM postings ORDER BY id")
        rows = {r["id"]: r for r in cur.fetchall()}
    for row in rows.values():
        assert row["company"] == "Acme Corporation"
        assert row["category"] == "Logistics"


def test_sweep_is_a_no_op_when_nothing_drifted():
    source_id = _insert_source("Acme Corporation", "Logistics")
    _insert_posting("p1", source_id, "Acme Corporation", "Logistics")

    with db.cursor() as cur:
        changed = run_source_sync_sweep(cur)
    assert changed == 0
