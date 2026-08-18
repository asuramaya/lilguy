"""The company / source / ats endpoints, and specifically the thing they
exist to get right: a company is NOT a source. For a direct ATS connector
the two coincide; for an aggregator one source carries many employers,
and the old UI had no way to express that.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2.extras
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ, reason="needs a scratch Postgres via DATABASE_URL"
)

import api  # noqa: E402
import db  # noqa: E402
from dedup import compute_company_key  # noqa: E402

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _clean_db():
    db.init_schema()
    with db.cursor() as cur:
        cur.execute("TRUNCATE postings, sources, scrape_runs RESTART IDENTITY CASCADE")
    yield


def _source(company, ats="greenhouse", status="active"):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (company, ats, config, status, category) "
            "VALUES (%s, %s, %s, %s, 'Logistics') RETURNING id",
            (company, ats, psycopg2.extras.Json({"company": company}), status),
        )
        return cur.fetchone()["id"]


def _posting(pid, company, source_id, ats="greenhouse", status="open",
             dedup_key=None, days_old=1, source_entry=None):
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_id, source_entry, company, company_key, title, location,
                                   url, ats, category, status, dedup_key, posted_at_ts, first_seen, last_seen)
            VALUES (%s, %s, %s, %s, %s, 'Ops Intern', 'Remote', %s, %s, 'Logistics', %s, %s, %s, now(), now())
            """,
            (pid, source_id, source_entry or company, company, compute_company_key(company),
             f"https://x/{pid}", ats, status, dedup_key, NOW - timedelta(days=days_old)),
        )


# --- company -----------------------------------------------------------

def test_company_unions_postings_arriving_through_different_sources():
    # The whole reason a company page can't just be a source page: this
    # employer is reachable via its own board AND via an aggregator.
    direct = _source("Eaton")
    muse = _source("The Muse (aggregator)", ats="muse")
    _posting("a", "Eaton", direct)
    _posting("b", "Eaton Corporation", muse, ats="muse", source_entry="The Muse (aggregator)")

    out = api.company(compute_company_key("Eaton"))
    assert len(out["postings"]) == 2
    assert {r["ats"] for r in out["reached_via"]} == {"greenhouse", "muse"}


def test_company_display_name_is_the_most_common_spelling():
    # company_key merges variants, so something must choose what to show.
    # Majority beats first-seen because raw URL slugs tend to be both.
    sid = _source("Eaton")
    _posting("a", "Eaton Corporation", sid)
    _posting("b", "Eaton Corporation", sid)
    _posting("c", "eaton", sid)
    out = api.company(compute_company_key("Eaton"))
    assert out["display_name"] == "Eaton Corporation"
    assert "eaton" in out["name_variants"]


def test_company_open_count_excludes_closed():
    sid = _source("Acme")
    _posting("a", "Acme", sid)
    _posting("b", "Acme", sid, status="closed")
    out = api.company(compute_company_key("Acme"))
    assert out["open_count"] == 1
    assert len(out["postings"]) == 2  # history still visible


def test_unknown_company_errors():
    assert "error" in api.company("nosuchcompany")


# --- posting -----------------------------------------------------------

def test_posting_surfaces_the_same_job_from_other_sources():
    # The point: a reader can see the job is also on the company's own
    # board and apply there rather than through an aggregator.
    direct = _source("Acme")
    muse = _source("The Muse (aggregator)", ats="muse")
    _posting("direct", "Acme", direct, dedup_key="k1")
    _posting("viamuse", "Acme", muse, ats="muse", dedup_key="k1", status="duplicate")

    out = api.posting("direct")
    assert [p["id"] for p in out["also_listed"]] == ["viamuse"]


def test_posting_lists_other_roles_at_the_same_company():
    sid = _source("Acme")
    _posting("a", "Acme", sid)
    _posting("b", "Acme", sid)
    _posting("elsewhere", "Globex", _source("Globex"))
    out = api.posting("a")
    assert [p["id"] for p in out["same_company"]] == ["b"]


def test_posting_same_company_excludes_closed_roles():
    sid = _source("Acme")
    _posting("a", "Acme", sid)
    _posting("b", "Acme", sid, status="closed")
    assert api.posting("a")["same_company"] == []


def test_unknown_posting_errors():
    assert "error" in api.posting("nope")


# --- source ------------------------------------------------------------

def test_direct_source_reports_one_company_and_offers_a_jump_key():
    sid = _source("Acme")
    _posting("a", "Acme", sid)
    out = api.source(sid)
    assert out["is_aggregator"] is False
    assert out["company_key"] == compute_company_key("Acme")
    assert out["stats"]["companies"] == 1


def test_aggregator_source_reports_many_companies():
    muse = _source("The Muse (aggregator)", ats="muse")
    for i, name in enumerate(["Acme", "Globex", "Initech"]):
        _posting(f"m{i}", name, muse, ats="muse", source_entry="The Muse (aggregator)")
    out = api.source(muse)
    assert out["is_aggregator"] is True
    assert out["stats"]["companies"] == 3
    assert {c["company"] for c in out["companies"]} == {"Acme", "Globex", "Initech"}


def test_source_jump_key_survives_a_board_with_no_open_postings():
    # A direct source with nothing posted right now is still that
    # company; deriving the key only from postings would drop the link
    # exactly when the board is empty.
    sid = _source("Acme")
    out = api.source(sid)
    assert out["company_key"] == compute_company_key("Acme")


def test_unknown_source_errors():
    assert "error" in api.source(999999)


def test_source_lists_its_open_postings_not_just_a_count():
    # "28 open postings" as a bare number is a dead end -- the page has to
    # name them, which is the whole reason to visit a board.
    muse = _source("The Muse (aggregator)", ats="muse")
    for i, name in enumerate(["Acme", "Globex", "Initech"]):
        _posting(f"m{i}", name, muse, ats="muse", days_old=i + 1)
    _posting("closed", "Acme", muse, ats="muse", status="closed")

    out = api.source(muse)
    assert [p["id"] for p in out["postings"]] == ["m0", "m1", "m2"], "newest first, open only"
    assert out["postings_truncated"] is False


def test_source_posting_list_is_bounded_and_says_so():
    # One aggregator row carries thousands; a page rendering all of them
    # is neither readable nor fast, and silently stopping short would
    # read as "that's all there is".
    muse = _source("The Muse (aggregator)", ats="muse")
    for i in range(api.SOURCE_POSTING_LIMIT + 5):
        _posting(f"m{i:04d}", f"Company {i}", muse, ats="muse")
    out = api.source(muse)
    assert len(out["postings"]) == api.SOURCE_POSTING_LIMIT
    assert out["postings_truncated"] is True


def test_source_reports_only_the_latest_run():
    sid = _source("Acme")
    with db.cursor() as cur:
        for i, ok in enumerate([True, False, True]):
            cur.execute(
                "INSERT INTO scrape_runs (source_id, started_at, ok, error, fetched_count, internship_count) "
                "VALUES (%s, now() - make_interval(hours => %s), %s, %s, 10, 2)",
                (sid, 3 - i, ok, None if ok else "boom"),
            )
    out = api.source(sid)
    assert out["latest_run"]["ok"] is True, "the most recent run, not the first stored"
    assert "recent_runs" not in out


def test_source_with_no_runs_yet_reports_none_rather_than_failing():
    sid = _source("Acme")
    assert api.source(sid)["latest_run"] is None


# --- company page carries the board health that the source page used to --

def test_company_reached_via_carries_board_health():
    # A direct board has no page of its own any more, so everything that
    # page uniquely showed has to survive here or folding them together
    # would lose it.
    sid = _source("Acme")
    _posting("a", "Acme", sid)
    with db.cursor() as cur:
        cur.execute("UPDATE sources SET consecutive_failures = 3 WHERE id = %s", (sid,))
        cur.execute(
            "INSERT INTO scrape_runs (source_id, started_at, ok, error, fetched_count, internship_count) "
            "VALUES (%s, now(), false, 'HTTP 500', 0, 0)",
            (sid,),
        )
    via = api.company(compute_company_key("Acme"))["reached_via"]
    assert len(via) == 1
    assert via[0]["consecutive_failures"] == 3
    assert via[0]["last_run_ok"] is False
    assert via[0]["last_run_error"] == "HTTP 500"


def test_company_reached_via_has_one_entry_per_board_not_per_run():
    # The health join is LATERAL over scrape_runs; without DISTINCT ON a
    # board with several runs would multiply its own row and the page
    # would list the same board repeatedly.
    sid = _source("Acme")
    _posting("a", "Acme", sid)
    with db.cursor() as cur:
        for i in range(4):
            cur.execute(
                "INSERT INTO scrape_runs (source_id, started_at, ok, fetched_count, internship_count) "
                "VALUES (%s, now() - make_interval(hours => %s), true, 1, 1)",
                (sid, i),
            )
    assert len(api.company(compute_company_key("Acme"))["reached_via"]) == 1


# --- categories --------------------------------------------------------

def _categorized(pid, category, source_id, status="open"):
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_id, source_entry, company, company_key, title, location,
                                   url, ats, category, status, first_seen, last_seen)
            VALUES (%s, %s, 'x', 'Acme', 'acme', 'Ops Intern', 'Remote', %s,
                    'greenhouse', %s, %s, now(), now())
            """,
            (pid, source_id, f"https://x/{pid}", category, status),
        )


def test_categories_rank_by_open_posting_count_not_alphabetically():
    sid = _source("Acme")
    for i in range(3):
        _categorized(f"log{i}", "Logistics", sid)
    _categorized("aero", "Aerospace & Defense", sid)
    # Rows are {value, open_postings}: the key is `value` rather than
    # `category` because one function now serves both axes, and naming it
    # after one of them would be a lie on the other.
    out = api.categories()["industries"]
    assert [c["value"] for c in out] == ["Logistics", "Aerospace & Defense"]
    assert out[0]["open_postings"] == 3


def test_categories_omit_ones_with_nothing_open():
    # A category whose every posting has closed is a dead end in the
    # picker: selecting it returns an empty feed and no explanation.
    sid = _source("Acme")
    _categorized("open", "Logistics", sid)
    _categorized("gone", "Semiconductors", sid, status="closed")
    assert [c["value"] for c in api.categories()["industries"]] == ["Logistics"]


def test_categories_ties_break_alphabetically_so_the_order_is_stable():
    sid = _source("Acme")
    _categorized("b", "Semiconductors", sid)
    _categorized("a", "Logistics", sid)
    assert [c["value"] for c in api.categories()["industries"]] == ["Logistics", "Semiconductors"]


# --- ats ---------------------------------------------------------------

def test_ats_summarizes_the_platform():
    a = _source("Acme")
    b = _source("Globex")
    _posting("a", "Acme", a)
    _posting("b", "Globex", b)
    out = api.ats("greenhouse")
    assert out["source_stats"]["sources"] == 2
    assert out["posting_stats"]["open_postings"] == 2
    assert out["posting_stats"]["companies"] == 2


def test_unknown_ats_errors():
    assert "error" in api.ats("nosuchats")


# --- the two category axes ---------------------------------------------

def _axis_posting(pid, source_id, category="", job_function="", ats="greenhouse"):
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_id, source_entry, company, company_key, title, location,
                                   url, ats, category, job_function, status, first_seen, last_seen)
            VALUES (%s, %s, 'x', 'Acme', 'acme', 'Ops Intern', 'Remote', %s, %s, %s, %s,
                    'open', now(), now())
            """,
            (pid, source_id, f"https://x/{pid}", ats, category, job_function),
        )


def test_categories_reports_both_axes_separately():
    sid = _source("Acme")
    _axis_posting("direct", sid, category="Aerospace & Defense")
    _axis_posting("agg", sid, job_function="Software Engineering", ats="muse")
    out = api.categories()
    assert [c["value"] for c in out["industries"]] == ["Aerospace & Defense"]
    assert [c["value"] for c in out["job_functions"]] == ["Software Engineering"]


def test_categories_reports_coverage_because_neither_axis_spans_the_corpus():
    # The UI needs these numbers to say out loud that an industry filter
    # excludes every aggregator posting. An invisible filter hiding most
    # of the corpus is the exact bug this split fixes.
    sid = _source("Acme")
    _axis_posting("a", sid, category="Banking")
    _axis_posting("b", sid, category="Banking")
    _axis_posting("c", sid, job_function="Sales", ats="muse")
    cov = api.categories()["coverage"]
    assert cov == {"total": 3, "with_industry": 2, "with_function": 1}


def test_the_two_axes_filter_independently():
    sid = _source("Acme")
    _axis_posting("direct", sid, category="Banking")
    _axis_posting("agg", sid, job_function="Sales", ats="muse")

    assert [p["id"] for p in api.feed(preset="all", category="Banking")["postings"]] == ["direct"]
    assert [p["id"] for p in api.feed(preset="all", job_function="Sales")["postings"]] == ["agg"]


def test_combining_both_axes_narrows_rather_than_widens():
    # AND, not OR. Today no posting carries both, so this is empty --
    # which is the honest answer, not a bug to paper over by
    # reinterpreting one axis as the other.
    sid = _source("Acme")
    _axis_posting("direct", sid, category="Banking")
    _axis_posting("agg", sid, job_function="Sales", ats="muse")
    assert api.feed(preset="all", category="Banking", job_function="Sales")["total"] == 0


def test_an_axis_with_nothing_open_is_omitted_from_its_list():
    sid = _source("Acme")
    _axis_posting("gone", sid, job_function="Sales", ats="muse")
    with db.cursor() as cur:
        cur.execute("UPDATE postings SET status = 'closed' WHERE id = 'gone'")
    assert api.categories()["job_functions"] == []


def test_security_headers_are_declared_for_forkers():
    # This deployment sits behind Tailscale, but the project exists to be
    # forked and a public deployment inherits whatever ships here.
    assert api.SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in api.SECURITY_HEADERS["Content-Security-Policy"]
    # The page is one static file with everything inline and no external
    # requests, so nothing is given up by forbidding other origins.
    assert "default-src 'self'" in api.SECURITY_HEADERS["Content-Security-Policy"]
