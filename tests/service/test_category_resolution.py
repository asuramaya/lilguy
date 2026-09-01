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


def test_a_permanently_unresolvable_source_cannot_starve_the_ones_behind_it():
    """The exact head-of-line shape already fixed once for the
    description backfill (test_workday_descriptions.py's own test of the
    same name), reproduced here: the first version of this sweep's claim
    query had no attempt tracking, so a source with no open postings (or
    postings with no classifiable title signal) stayed instantly
    re-claimable at the head of `ORDER BY s.id` and every cycle re-fetched
    the SAME lowest-id source forever, never reaching a resolvable one
    behind it. Confirmed live against the real backlog before this fix:
    30 consecutive batches, 0 fixed, every time.
    """
    stuck = _source("stuckco")  # zero postings, unresolvable -- lower id, sits at the head
    victim = _source("aecom2")  # resolvable -- higher id, sits behind it
    _open_posting(victim, "Architecture Design Intern", path="/a")
    _open_posting(victim, "Civil Engineering Intern", path="/b")

    first = cat_res.run(limit=1)
    assert first["skipped"] == 1
    assert _category(victim)["category"] == "Uncategorized"  # never reached yet

    second = cat_res.run(limit=1)
    assert second["fixed"] == 1, "the queue must advance past a source that can never resolve"
    assert _category(victim)["category"] != "Uncategorized"


def test_backoff_lengthens_with_repeated_skip_and_is_capped():
    sid = _source("stuckco")
    for _ in range(len(cat_res.BACKOFF_HOURS) + 2):
        out = cat_res.run(limit=5)
        assert out["skipped"] == 1
        _rewind(sid)

    with db.cursor() as cur:
        cur.execute("SELECT category_attempts FROM sources WHERE id = %s", (sid,))
        assert cur.fetchone()["category_attempts"] == len(cat_res.BACKOFF_HOURS) + 2

    # Capped rather than unbounded: a source that never resolves must
    # still come back around eventually, just never often enough to
    # monopolise the queue.
    with db.cursor() as cur:
        cur.execute("SELECT category_next_attempt_at - now() AS gap FROM sources WHERE id = %s", (sid,))
        gap_hours = cur.fetchone()["gap"].total_seconds() / 3600
    assert gap_hours <= max(cat_res.BACKOFF_HOURS) + 1


def _rewind(source_id):
    """Pretend the backoff window has elapsed."""
    with db.cursor() as cur:
        cur.execute(
            "UPDATE sources SET category_next_attempt_at = now() - interval '1 minute' WHERE id = %s",
            (source_id,),
        )
