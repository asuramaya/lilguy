"""Covers /feed's server-side narrowing and paging.

Calls the endpoint function directly rather than over HTTP: it's an
ordinary Python function, and the behaviour under test is the filtering
and paging logic, not FastAPI's routing (testing over HTTP would also
add an httpx dependency the service doesn't otherwise need).
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

import api  # noqa: E402
import db  # noqa: E402

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _clean_db():
    db.init_schema()
    with db.cursor() as cur:
        cur.execute("TRUNCATE postings, sources RESTART IDENTITY CASCADE")
    yield


def _posting(id_, title, company, category="Logistics", days_old=1):
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_entry, company, title, location, url, ats,
                                   category, status, posted_at_ts, first_seen, last_seen)
            VALUES (%s, 'x', %s, %s, 'Remote', %s, 'greenhouse', %s, 'open', %s, now(), now())
            """,
            (id_, company, title, f"https://x/{id_}", category, NOW - timedelta(days=days_old)),
        )


def test_all_returns_the_unfiltered_corpus_with_a_total():
    _posting("a", "Supply Chain Intern", "Acme")
    _posting("b", "Backend Engineering Intern", "Globex")
    out = api.feed(preset="all")
    assert out["total"] == 2
    assert out["count"] == 2


def test_search_matches_title_or_company_across_the_whole_corpus():
    _posting("a", "Supply Chain Intern", "Acme")
    _posting("b", "Backend Engineering Intern", "Globex")
    _posting("c", "Marketing Intern", "Logistics Partners Ltd")

    by_title = api.feed(preset="all", q="supply")
    assert [p["id"] for p in by_title["postings"]] == ["a"]

    # Company-side match: the needle appears only in the company name.
    by_company = api.feed(preset="all", q="logistics partners")
    assert [p["id"] for p in by_company["postings"]] == ["c"]

    assert api.feed(preset="all", q="nothing-matches-this")["total"] == 0


def test_search_is_case_insensitive():
    _posting("a", "Supply Chain Intern", "Acme")
    assert api.feed(preset="all", q="SUPPLY CHAIN")["total"] == 1


def test_category_filter_is_exact():
    _posting("a", "Intern One", "Acme", category="Logistics")
    _posting("b", "Intern Two", "Globex", category="Semiconductors")
    out = api.feed(preset="all", category="Semiconductors")
    assert [p["id"] for p in out["postings"]] == ["b"]


def test_paging_reports_total_independently_of_the_page():
    for i in range(5):
        _posting(f"p{i}", f"Intern {i}", "Acme", days_old=i + 1)

    page = api.feed(preset="all", limit=2, offset=0)
    assert page["total"] == 5          # the whole match...
    assert page["count"] == 2          # ...and what this page holds
    assert page["offset"] == 0

    second = api.feed(preset="all", limit=2, offset=2)
    assert second["count"] == 2
    first_ids = {p["id"] for p in page["postings"]}
    second_ids = {p["id"] for p in second["postings"]}
    assert not (first_ids & second_ids), "pages overlap"

    tail = api.feed(preset="all", limit=2, offset=4)
    assert tail["count"] == 1


def test_results_are_ordered_by_real_posted_date_newest_first():
    _posting("old", "Intern Old", "Acme", days_old=400)
    _posting("new", "Intern New", "Acme", days_old=1)
    _posting("mid", "Intern Mid", "Acme", days_old=30)
    out = api.feed(preset="all")
    assert [p["id"] for p in out["postings"]] == ["new", "mid", "old"]


def test_undated_postings_sort_last_not_first():
    # A posting with no provider date must not head a "newest first"
    # list purely because its date column is empty.
    _posting("dated", "Intern Dated", "Acme", days_old=10)
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_entry, company, title, location, url, ats,
                                   category, status, posted_at_ts, first_seen, last_seen)
            VALUES ('undated', 'x', 'Acme', 'Intern Undated', 'Remote', 'https://x/u',
                    'greenhouse', 'Logistics', 'open', NULL, now(), now())
            """
        )
    out = api.feed(preset="all")
    assert [p["id"] for p in out["postings"]] == ["dated", "undated"]


def test_max_age_days_filters_on_the_employers_date_not_discovery_time():
    # Every row here has first_seen = now, so filtering on first_seen
    # would keep both. Only posted_at_ts distinguishes them -- this is
    # the bug that let decade-old listings through a freshness filter.
    _posting("fresh", "Intern Fresh", "Acme", days_old=3)
    _posting("stale", "Intern Stale", "Acme", days_old=400)
    out = api.feed(preset="all", max_age_days=30)
    assert [p["id"] for p in out["postings"]] == ["fresh"]


def test_unknown_preset_still_errors():
    assert "error" in api.feed(preset="definitely-not-a-preset")


def test_paging_never_repeats_or_skips_when_sort_keys_tie():
    # Without a unique final sort key the ordering is not total, and
    # Postgres may return tied rows in a different order per query --
    # so offset paging repeats some and drops others. Found live: 88
    # open postings shared one (posted_at_ts, first_seen) pair and three
    # pages of 200 yielded 592 distinct rows out of 600.
    same_instant = NOW - timedelta(days=5)
    with db.cursor() as cur:
        for i in range(25):
            cur.execute(
                """
                INSERT INTO postings (id, source_entry, company, title, location, url, ats,
                                       category, status, posted_at_ts, first_seen, last_seen)
                VALUES (%s, 'x', 'Acme', %s, 'Remote', 'https://x', 'greenhouse',
                        'Logistics', 'open', %s, %s, now())
                """,
                (f"tie{i:03d}", f"Intern {i}", same_instant, same_instant),
            )

    seen = []
    for offset in range(0, 25, 5):
        page = api.feed(preset="all", limit=5, offset=offset)
        seen.extend(p["id"] for p in page["postings"])

    assert len(seen) == 25
    assert len(set(seen)) == 25, "offset paging repeated or skipped rows across tied sort keys"


def test_feed_does_not_ship_full_descriptions():
    # 65% of the feed payload was description text the feed never
    # renders -- 12 MB of a 16 MB read per request, measured live. The
    # detail endpoint is where the full text belongs.
    _posting("a", "Supply Chain Intern", "Acme")
    with db.cursor() as cur:
        cur.execute("UPDATE postings SET description = %s WHERE id = 'a'", ("x" * 5000,))

    row = api.feed(preset="all")["postings"][0]
    assert "description" not in row
    assert "description_snippet" in row, "the SHORT form must survive -- the table renders it"
    assert api.posting("a")["posting"]["description"] == "x" * 5000, \
        "the detail page must still get the full text"


def test_feed_columns_track_the_schema_instead_of_a_hardcoded_list():
    # Derived from information_schema so a new column reaches the API
    # automatically. If someone adds one that should NOT be public, they
    # add it to FEED_EXCLUDED_COLUMNS -- the point is that the omission
    # is a decision someone wrote down, not an oversight.
    _posting("a", "Intern", "Acme")
    row = api.feed(preset="all")["postings"][0]
    with db.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'postings'")
        all_columns = {r["column_name"] for r in cur.fetchall()}
    assert set(row) == all_columns - api.FEED_EXCLUDED_COLUMNS


def test_retry_bookkeeping_stays_internal():
    _posting("a", "Intern", "Acme")
    row = api.feed(preset="all")["postings"][0]
    assert "description_attempts" not in row
    assert "description_next_attempt_at" not in row
