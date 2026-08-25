"""category_resolution.py exists to pay down the 'Uncategorized' backlog
mechanically -- discovery.py seeds every new Common-Crawl candidate with
category="Uncategorized" and nothing else ever fills it in. Measured
live against the real 437-source backlog: company-name matching alone
(standardize.infer_category's own heuristics) resolved only 3.2% of it,
because most of these companies are raw lowercase tenant slugs
("aecom2", "cvshealth") that defeat word-boundary regexes. Deriving
job_function from each posting's TITLE first and feeding that into
infer_category resolved 49.5% instead -- these tests cover the sweep
itself (claiming, voting, writing), not infer_category/
standardize_job_function's own per-case logic, which test_standardize.py
already covers.
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
import category_resolution as cat_res  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    db.init_schema()
    with db.cursor() as cur:
        cur.execute("TRUNCATE postings, sources RESTART IDENTITY CASCADE")
    yield


def _source(company, category="Uncategorized"):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (company, ats, config, category, status) "
            "VALUES (%s, 'workday', %s, %s, 'active') RETURNING id",
            (company, psycopg2.extras.Json({"category": category}), category),
        )
        return cur.fetchone()["id"]


def _open_posting(source_id, title, snippet="", path=None):
    path = path or f"/job/{title.replace(' ', '-')}"
    pid = f"workday:acme:External:{path}"
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_id, source_entry, company, title, location, url, ats,
                                   category, description_snippet, status, first_seen, last_seen)
            VALUES (%s, %s, 'Acme', 'Acme', %s, 'Remote', %s, 'workday', 'Uncategorized', %s, 'open', now(), now())
            """,
            (pid, source_id, title, f"https://x{pid}", snippet),
        )
    return pid


def _category(source_id):
    with db.cursor() as cur:
        cur.execute("SELECT category, config->>'category' AS config_category FROM sources WHERE id = %s", (source_id,))
        return cur.fetchone()


def test_a_source_gets_categorized_when_most_titles_agree():
    sid = _source("aecom2")
    _open_posting(sid, "Architecture Design Intern", path="/a")
    _open_posting(sid, "Civil Engineering Intern", path="/b")
    _open_posting(sid, "Structural Engineering Intern", path="/c")
    out = cat_res.run(limit=5)
    assert out["fixed"] == 1
    row = _category(sid)
    assert row["category"] != "Uncategorized"
    # config->>'category' must stay in sync -- it's what connectors
    # actually read on the NEXT scrape (see scheduler.py's run_one).
    assert row["config_category"] == row["category"]


def test_a_source_is_left_alone_when_titles_disagree_with_no_majority():
    sid = _source("diversifiedco")
    _open_posting(sid, "Software Engineer Intern", path="/a")
    _open_posting(sid, "Financial Analyst Intern", path="/b")
    _open_posting(sid, "Marketing Intern", path="/c")
    out = cat_res.run(limit=5)
    assert out["fixed"] == 0
    assert out["skipped"] == 1
    assert _category(sid)["category"] == "Uncategorized"


def test_a_source_with_fewer_than_min_samples_is_left_unresolved():
    # A single resolving title is still just one posting's say-so.
    sid = _source("aecom2")
    _open_posting(sid, "Architecture Design Intern")
    out = cat_res.run(limit=5)
    assert out["fixed"] == 0
    assert out["skipped"] == 1
    assert _category(sid)["category"] == "Uncategorized"


def test_a_source_with_no_open_postings_is_skipped_not_crashed():
    sid = _source("aecom2")
    out = cat_res.run(limit=5)
    assert out["attempted"] == 1
    assert out["skipped"] == 1
    assert _category(sid)["category"] == "Uncategorized"


def test_titles_that_dont_resolve_to_anything_dont_prevent_a_majority():
    # A title with no signal (e.g. a generic "Intern") is a missing vote,
    # not a vote against the majority the other postings do agree on.
    sid = _source("aecom2")
    _open_posting(sid, "Architecture Design Intern", path="/a")
    _open_posting(sid, "Civil Engineering Intern", path="/b")
    _open_posting(sid, "Intern", path="/c")
    out = cat_res.run(limit=5)
    assert out["fixed"] == 1


def test_an_already_categorized_source_is_never_claimed():
    sid = _source("nvidia", category="Semiconductors & AI Hardware")
    out = cat_res.run(limit=5)
    assert out["attempted"] == 0
    assert _category(sid)["category"] == "Semiconductors & AI Hardware"


def test_a_fixed_source_is_not_reclaimed_on_a_second_pass():
    sid = _source("aecom2")
    _open_posting(sid, "Architecture Design Intern", path="/a")
    _open_posting(sid, "Civil Engineering Intern", path="/b")
    cat_res.run(limit=5)
    out = cat_res.run(limit=5)
    assert out["attempted"] == 0
