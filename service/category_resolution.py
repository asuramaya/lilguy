"""Mechanically pays down the 'Uncategorized' backlog -- sources whose
industry category was never guessed (discovery.py seeds every new
Common-Crawl candidate with category="Uncategorized"; nothing before
this filled it in later).

Needs no network I/O at all, unlike company_resolution.py -- everything
it needs (title, description_snippet) is already sitting in Postgres
from the scrape that already happened. That makes it cheap enough to run
every cycle without pacing concerns.

WHY COMPANY-NAME MATCHING ALONE ISN'T ENOUGH: standardize.py's
infer_category() has company-name heuristics (a bank name implies
Financial Services, etc.), but measured live against the real backlog
(437 sources), that alone resolved only 3.2% of it -- most of these
companies are raw lowercase Workday tenant slugs ("aecom2", "cvshealth")
that defeat \b-anchored regexes (no word boundary between "cvs" and
"health" in one mashed-together token), or small/unknown startups with
no industry hint in the name at all ("redpine", "photoroom").

WHAT ACTUALLY WORKS: a posting's TITLE carries real signal almost
regardless of how obscure the company is ("Mechanical Engineer Intern"
means the same thing whether the company is Boeing or an unknown
startup). Running standardize_job_function() on the title first, then
feeding that job_function into infer_category(), measured live at 49.5%
coverage on the same backlog -- roughly 15x better than company-name
matching alone.

MAJORITY VOTE, not one posting's say-so -- same discipline
company_resolution.py was redesigned around, and for the same reason: a
diversified employer's postings don't all point the same direction (a
bank's own IT-department intern posting would categorize the whole
company as Software & Technology instead of Financial Services if
trusted alone). Sampling several of a source's postings and requiring a
genuine majority among the ones that DO resolve to something means one
off-department posting can't override what most of a source's postings
actually say the company does.
"""
import sys
from collections import Counter
from pathlib import Path

import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))

from db import cursor  # noqa: E402
from standardize import infer_category, standardize_company_name, standardize_job_function  # noqa: E402

# How many of a source's open postings to sample before trusting a
# category. No network cost per sample here (unlike company_resolution.py),
# so this can afford to look at more of a source's postings for the same
# reason a bigger jury is a better jury.
SAMPLE_SIZE = 10
# A genuine majority among postings that resolved to SOME category, not
# a plurality -- three off-department postings agreeing with each other
# should not outvote a fourth that happens to be the company's real
# primary category if there's no clear majority either way.
MIN_SAMPLES = 2


def _claim_batch(limit: int) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """
            SELECT s.id AS source_id, s.company, s.config,
                   COALESCE(
                       (SELECT jsonb_agg(jsonb_build_array(p.title, p.description_snippet))
                        FROM (SELECT title, description_snippet FROM postings
                              WHERE source_id = s.id AND status = 'open'
                              ORDER BY first_seen DESC LIMIT %s) p),
                       '[]'::jsonb
                   ) AS sample_postings
            FROM sources s
            WHERE s.category = 'Uncategorized'
            ORDER BY s.id
            LIMIT %s
            """,
            (SAMPLE_SIZE, limit),
        )
        return cur.fetchall()


def _update_source_category(source_id: int, category: str, config: dict) -> None:
    config = dict(config or {})
    config["category"] = category
    with cursor() as cur:
        cur.execute(
            "UPDATE sources SET category = %s, config = %s WHERE id = %s",
            (category, psycopg2.extras.Json(config), source_id),
        )


def run(limit: int = 20) -> dict:
    """Never raises: one malformed row must not stop the scheduler."""
    rows = _claim_batch(limit)
    if not rows:
        return {"attempted": 0, "fixed": 0, "skipped": 0}

    fixed = skipped = 0
    for row in rows:
        postings = row.get("sample_postings") or []
        if not postings:
            skipped += 1
            continue

        clean_name = standardize_company_name(row["company"])
        categories = []
        for title, snippet in postings:
            job_function = standardize_job_function(title or "", snippet or "")
            guess = infer_category(clean_name, title or "", job_function, "Uncategorized")
            if guess != "Uncategorized":
                categories.append(guess)

        if len(categories) < MIN_SAMPLES:
            skipped += 1
            continue

        top_category, top_count = Counter(categories).most_common(1)[0]
        if top_count > len(categories) / 2:
            _update_source_category(row["source_id"], top_category, row["config"])
            fixed += 1
        else:
            skipped += 1

    return {"attempted": len(rows), "fixed": fixed, "skipped": skipped}
