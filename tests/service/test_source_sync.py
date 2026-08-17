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
    # Two row-updates for one posting: company drifted, and company_key
    # is derived from company so it necessarily drifted too.
    assert changed == 2

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
    assert changed == 4  # two postings x (fields + derived key)

    with db.cursor() as cur:
        cur.execute("SELECT id, company, category FROM postings ORDER BY id")
        rows = {r["id"]: r for r in cur.fetchall()}
    for row in rows.values():
        assert row["company"] == "Acme Corporation"
        assert row["category"] == "Logistics"


def test_sweep_is_idempotent():
    # The first run legitimately sets company_key, which starts unset.
    # The guarantee worth pinning is that running it again changes
    # nothing -- that's what makes it safe on every scheduler cycle.
    source_id = _insert_source("Acme Corporation", "Logistics")
    _insert_posting("p1", source_id, "Acme Corporation", "Logistics")

    with db.cursor() as cur:
        run_source_sync_sweep(cur)
    with db.cursor() as cur:
        assert run_source_sync_sweep(cur) == 0


def test_sql_company_key_agrees_with_the_python_implementation():
    # The sweep recomputes company_key with SQL mirrored from
    # dedup.compute_company_key, because it's a set-based UPDATE over the
    # whole table. Two implementations of one rule drift silently, so
    # this pins them together on inputs drawn from the real corpus --
    # legal suffixes, punctuation, casing, raw slugs and unicode.
    from dedup import compute_company_key

    names = [
        "Eaton", "Eaton Corporation", "Samsara Inc.", "Acme, Inc.",
        "A.P. Moller - Maersk", "JLL (Jones Lang LaSalle)", "GE Aerospace",
        "geaerospace", "The Vita Coco Company", "Reckitt (Reckitt Benckiser)",
        "MUFG (Mitsubishi UFJ Financial Group)", "3M", "66degrees",
        "International Flavors & Fragrances", "djeholdings", "ag",
        "Kraft Heinz Co", "Brookfield Asset Management Ltd",
    ]
    source_id = _insert_source("Placeholder", "Cat")
    for i, name in enumerate(names):
        _insert_posting(f"p{i}", source_id, name, "Cat")

    # Detach from the source so the sweep's company= copy doesn't
    # overwrite the varied names we're testing.
    with db.cursor() as cur:
        cur.execute("UPDATE postings SET source_id = NULL")
        run_source_sync_sweep(cur)

    with db.cursor() as cur:
        cur.execute("SELECT company, company_key FROM postings ORDER BY id")
        rows = cur.fetchall()

    mismatches = [(r["company"], r["company_key"], compute_company_key(r["company"]))
                  for r in rows if r["company_key"] != compute_company_key(r["company"])]
    assert not mismatches, f"SQL and Python company_key disagree: {mismatches}"


def test_company_key_unites_spelling_variants_of_one_employer():
    source_id = _insert_source("Placeholder", "Cat")
    _insert_posting("a", source_id, "Eaton", "Cat")
    _insert_posting("b", source_id, "Eaton Corporation", "Cat")
    with db.cursor() as cur:
        cur.execute("UPDATE postings SET source_id = NULL")
        run_source_sync_sweep(cur)
        cur.execute("SELECT DISTINCT company_key FROM postings")
        assert len(cur.fetchall()) == 1


def _insert_aggregator_posting(id_, source_id, real_employer):
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_id, source_entry, company, title, location, url,
                                   ats, category, status, dedup_key, first_seen, last_seen)
            VALUES (%s, %s, 'The Muse (aggregator)', %s, 'Intern', 'Remote', 'https://x',
                    'muse', 'Logistics', 'open', %s, now(), now())
            """,
            (id_, source_id, real_employer, f"dk:{id_}"),
        )


def test_sweep_never_overwrites_the_employer_on_an_aggregator_posting():
    # THE bug this guard exists for, found live: `company` is
    # source-derived for a direct connector (it comes from our config and
    # is constant per source) but job-derived for an aggregator -- muse
    # reads it from each posting. Syncing every row from sources.company
    # flattened all 2798 open Muse postings to the board's own label,
    # "The Muse (aggregator ...)", which is not a company at all. It would
    # also have fought the upsert forever: the next fetch restores the
    # real name, the next sweep destroys it again.
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (company, ats, category, config, status) "
            "VALUES ('The Muse (aggregator)', 'muse', 'Aggregated', %s, 'active') RETURNING id",
            (psycopg2.extras.Json({}),),
        )
        muse_id = cur.fetchone()["id"]

    _insert_aggregator_posting("m1", muse_id, "Rocket Lab")
    _insert_aggregator_posting("m2", muse_id, "Vita Coco")

    with db.cursor() as cur:
        run_source_sync_sweep(cur)

    with db.cursor() as cur:
        cur.execute("SELECT id, company FROM postings ORDER BY id")
        got = {r["id"]: r["company"] for r in cur.fetchall()}
    assert got == {"m1": "Rocket Lab", "m2": "Vita Coco"}


def test_sweep_still_syncs_direct_ats_postings():
    # The guard must not disable the sweep's actual job for the sources
    # where company genuinely is source-derived.
    source_id = _insert_source("Acme Corporation", "Logistics")
    _insert_posting("d1", source_id, "acmecorp", "Uncategorized")
    with db.cursor() as cur:
        run_source_sync_sweep(cur)
    with db.cursor() as cur:
        cur.execute("SELECT company, category FROM postings WHERE id = 'd1'")
        r = cur.fetchone()
    assert r["company"] == "Acme Corporation"
    assert r["category"] == "Logistics"
