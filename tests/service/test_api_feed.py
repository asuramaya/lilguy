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


# --- full-text search ---------------------------------------------------

def _described(pid, title, company, description, location="Remote"):
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_entry, company, title, location, url, ats,
                                   category, status, description, posted_at_ts, first_seen, last_seen)
            VALUES (%s, 'x', %s, %s, %s, %s, 'greenhouse', 'Logistics', 'open', %s,
                    now(), now(), now())
            """,
            (pid, company, title, location, f"https://x/{pid}", description),
        )


def test_search_finds_words_that_only_appear_in_the_description():
    # The gap this closes: descriptions were the corpus's best data and
    # search could not see a word of them.
    _described("ops", "Operations Intern", "Acme",
               "You will own our supply chain planning and logistics network.")
    _described("other", "Marketing Intern", "Globex", "Social media and brand work.")
    out = api.feed(preset="all", q="supply chain")
    assert [p["id"] for p in out["postings"]] == ["ops"]


def test_a_title_hit_outranks_a_description_mention():
    _described("body", "Operations Intern", "Acme",
               "Supply chain exposure across our logistics network.")
    _described("titled", "Supply Chain Intern", "Globex", "General duties.")
    out = api.feed(preset="all", q="supply chain")
    assert out["postings"][0]["id"] == "titled", "the job CALLED that should come first"
    assert {p["id"] for p in out["postings"]} == {"titled", "body"}


def test_search_finds_skills_that_never_appear_in_a_title():
    _described("py", "Data Intern", "Acme", "Strong Python and SQL skills required.")
    _described("cad", "Design Intern", "Globex", "Proficiency with CAD tooling.")
    assert [p["id"] for p in api.feed(preset="all", q="python")["postings"]] == ["py"]
    assert [p["id"] for p in api.feed(preset="all", q="CAD")["postings"]] == ["cad"]


def test_partial_words_still_match_so_search_did_not_lose_an_ability():
    # Full-text matches whole words, so "eng" would stop finding
    # "Engineering". The substring pass is kept as a floor precisely so
    # this change is strictly additive.
    _described("eng", "Engineering Intern", "Acme", "Build things.")
    assert [p["id"] for p in api.feed(preset="all", q="eng")["postings"]] == ["eng"]


def test_search_matches_location():
    _described("chi", "Ops Intern", "Acme", "General duties.", location="Chicago, IL")
    _described("nyc", "Ops Intern", "Globex", "General duties.", location="New York, NY")
    assert [p["id"] for p in api.feed(preset="all", q="Chicago")["postings"]] == ["chi"]


def test_search_survives_punctuation_that_would_break_a_raw_tsquery():
    # websearch_to_tsquery accepts whatever a person types; to_tsquery
    # would turn these into a 500.
    _described("a", "Ops Intern", "Acme", "General duties.")
    for hostile in ["'", "&&", "a | b", '"unclosed', ":*", "!"]:
        out = api.feed(preset="all", q=hostile)
        assert "error" not in out, f"query {hostile!r} broke the endpoint"


def test_browsing_without_a_query_stays_newest_first():
    # Relevance is for searching; recency is for browsing.
    _described("old", "Ops Intern", "Acme", "x")
    _described("new", "Ops Intern", "Globex", "x")
    with db.cursor() as cur:
        cur.execute("UPDATE postings SET posted_at_ts = now() - interval '400 days' WHERE id = 'old'")
    assert [p["id"] for p in api.feed(preset="all")["postings"]] == ["new", "old"]


def test_search_vector_is_maintained_by_the_database_not_the_caller():
    # Written with a plain INSERT that never mentions search_vector: if
    # this ever needs application code to populate it, a connector added
    # later will forget.
    _described("gen", "Ops Intern", "Acme", "Warehouse automation robotics.")
    assert [p["id"] for p in api.feed(preset="all", q="robotics")["postings"]] == ["gen"]

    with db.cursor() as cur:
        cur.execute("UPDATE postings SET description = 'Completely different: hydraulics.' WHERE id = 'gen'")
    assert api.feed(preset="all", q="robotics")["total"] == 0, "stale vector after an update"
    assert [p["id"] for p in api.feed(preset="all", q="hydraulics")["postings"]] == ["gen"]


# --- corpus cache -------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_corpus_cache():
    # The cache is process-local and survives between tests, which would
    # let one test's corpus leak into the next.
    api._corpus_cache.update(rows=None, fetched_at=0.0, truncated=False)
    yield
    api._corpus_cache.update(rows=None, fetched_at=0.0, truncated=False)


def test_the_corpus_read_is_cached_across_requests():
    # The point of the cache: a flood of DISTINCT queries costs one
    # database read per TTL rather than one per request. A response
    # cache keyed on the query string would be bypassed by ?q=<random>.
    _posting("a", "Supply Chain Intern", "Acme")
    api.feed(preset="all")

    # Written straight to the database, bypassing the cache.
    _posting("b", "Second Intern", "Globex")
    assert api.feed(preset="all")["total"] == 1, "second request re-read the corpus"
    assert api.feed(preset="all", q="second")["total"] == 0, "a distinct query bypassed the cache"


def test_the_cache_expires_so_new_postings_appear():
    _posting("a", "Supply Chain Intern", "Acme")
    api.feed(preset="all")
    _posting("b", "Second Intern", "Globex")

    api._corpus_cache["fetched_at"] -= api.CORPUS_TTL_SECONDS + 1
    assert api.feed(preset="all")["total"] == 2


def test_filtering_never_mutates_the_shared_snapshot():
    # Every caller gets the SAME list object, so a filter that mutated
    # it would corrupt the corpus for every later request.
    for i in range(3):
        _posting(f"p{i}", f"Intern {i}", "Acme", days_old=i + 1)
    api.feed(preset="all")
    before = list(api._corpus_cache["rows"])
    api.feed(preset="all", q="intern", category="Logistics", max_age_days=5, limit=1)
    assert api._corpus_cache["rows"] == before


# --- hiring cycle -------------------------------------------------------

def _cycled(pid, title, season="", year=None):
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_entry, company, title, location, url, ats,
                                   category, status, cycle_season, cycle_year,
                                   posted_at_ts, first_seen, last_seen)
            VALUES (%s, 'x', 'Acme', %s, 'Remote', %s, 'greenhouse', 'Logistics', 'open',
                    %s, %s, now(), now(), now())
            """,
            (pid, title, f"https://x/{pid}", season, year),
        )


def test_cycle_filter_matches_season_and_year():
    _cycled("s27", "Summer 2027 Intern", "summer", 2027)
    _cycled("f27", "Fall 2027 Intern", "fall", 2027)
    _cycled("s26", "Summer 2026 Intern", "summer", 2026)

    out = api.feed(preset="all", cycle_season="summer", cycle_year=2027,
                   include_unstated_cycle=False)
    assert [p["id"] for p in out["postings"]] == ["s27"]


def test_postings_with_no_stated_cycle_are_kept_but_separated():
    # 60% of titles state no cycle. Excluding them would hide postings
    # that may well be the cycle asked for; mixing them in would leave a
    # filter whose results are mostly unconfirmed.
    _cycled("stated", "Summer 2027 Intern", "summer", 2027)
    _cycled("silent", "Operations Intern")

    out = api.feed(preset="all", cycle_season="summer", cycle_year=2027)
    assert out["total"] == 2
    assert out["unstated_cycle"] == 1
    assert [p["id"] for p in out["postings"]] == ["stated", "silent"], \
        "confirmed matches must come before the unconfirmed ones"


def test_unstated_cycle_postings_can_be_excluded_outright():
    _cycled("stated", "Summer 2027 Intern", "summer", 2027)
    _cycled("silent", "Operations Intern")
    out = api.feed(preset="all", cycle_year=2027, include_unstated_cycle=False)
    assert out["total"] == 1
    assert out["unstated_cycle"] == 0


def test_a_posting_stating_the_wrong_cycle_is_excluded_not_treated_as_unknown():
    # The distinction that makes the separate group honest: "Summer 2026"
    # is a real answer, and it is NO. It must not be quietly rescued into
    # the unconfirmed group.
    _cycled("wrong", "Summer 2026 Intern", "summer", 2026)
    out = api.feed(preset="all", cycle_year=2027)
    assert out["total"] == 0


def test_season_matching_is_case_insensitive():
    _cycled("s", "Summer 2027 Intern", "summer", 2027)
    assert api.feed(preset="all", cycle_season="Summer", include_unstated_cycle=False)["total"] == 1


def test_no_cycle_filter_reports_zero_unstated():
    _cycled("a", "Operations Intern")
    assert api.feed(preset="all")["unstated_cycle"] == 0


def test_categories_lists_cycle_years_in_timeline_order():
    # Ascending, unlike the other facets: a cycle list is a timeline, not
    # a popularity ranking.
    _cycled("a", "x", "summer", 2027)
    _cycled("b", "x", "summer", 2026)
    _cycled("c", "x", "summer", 2026)
    out = api.categories()
    assert [y["value"] for y in out["cycle_years"]] == [2026, 2027]
    assert out["coverage"]["with_cycle"] == 3


# --- work arrangement ---------------------------------------------------

def _arranged(pid, arrangement, location="Remote"):
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_entry, company, title, location, url, ats,
                                   category, status, work_arrangement,
                                   posted_at_ts, first_seen, last_seen)
            VALUES (%s, 'x', 'Acme', 'Ops Intern', %s, %s, 'greenhouse', 'Logistics', 'open',
                    %s, now(), now(), now())
            """,
            (pid, location, f"https://x/{pid}", arrangement),
        )


def test_work_arrangement_filters_exactly():
    _arranged("r", "remote")
    _arranged("h", "hybrid")
    _arranged("o", "onsite")
    assert [p["id"] for p in api.feed(preset="all", work_arrangement="remote")["postings"]] == ["r"]
    assert [p["id"] for p in api.feed(preset="all", work_arrangement="hybrid")["postings"]] == ["h"]


def test_postings_with_no_stated_arrangement_are_never_claimed_as_onsite():
    # A blank means "no source said", not "onsite". Defaulting the
    # unknown to a value would be exactly the invention the industry /
    # function split refused.
    _arranged("blank", "", location="Austin, TX")
    assert api.feed(preset="all", work_arrangement="onsite")["total"] == 0
    assert api.feed(preset="all")["total"] == 1


def test_arrangement_filter_is_case_insensitive():
    _arranged("r", "remote")
    assert api.feed(preset="all", work_arrangement="Remote")["total"] == 1


def test_categories_reports_arrangement_coverage():
    _arranged("r", "remote")
    _arranged("blank", "", location="Austin, TX")
    out = api.categories()
    assert [a["value"] for a in out["work_arrangements"]] == ["remote"]
    assert out["coverage"]["with_arrangement"] == 1
