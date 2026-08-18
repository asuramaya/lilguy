"""Guards a bug class that has now bitten four times.

Every instance had one shape: an ORDER BY without a unique final column,
paged with LIMIT/OFFSET. Postgres may order tied rows differently between
queries, so pages silently repeat some rows and drop others, and the
caller gets a plausible-looking list with no error.

  - /feed paging        -- 88 postings shared one (posted_at_ts, first_seen);
                           three pages of 200 returned 592 distinct rows of 600
  - description backfill claim -- rows from one scrape share first_seen
  - liveness claim             -- same
  - /candidates paging         -- rows share checked_at

THIS IS A STATIC CHECK, deliberately, and the reason is worth recording.
I first wrote it as a behavioural test: build a corpus where every sort
key ties, page through it, assert nothing repeats. It passed against a
deliberately BROKEN ORDER BY -- twice. Once because /feed pages out of an
in-process snapshot so the SQL order never re-runs, and again, after
expiring that cache between pages, because Postgres's sort over a small
static table is stable in practice. The corruption needs production
scale and a moving heap to appear.

A test that cannot fail on the bug it names is worse than no test: it
converts an open question into false confidence. So this asserts the
INVARIANT instead -- every paged query ends its ORDER BY on a unique
column -- which is deterministic, and is the actual rule being broken.
"""
import re
from pathlib import Path

import pytest

API = Path(__file__).parent.parent.parent / "service" / "api.py"

# Columns unique per row in the table each query pages over.
# `company` qualifies only because `sources` is UNIQUE (company, ats) and
# every query ordering by it also filters to one ats.
UNIQUE_COLUMNS = ("id", "p.id", "d.id", "s.id", "company_key", "company")

# ORDER BY ... LIMIT, bounded so the match cannot run from one statement
# into a LIMIT belonging to another. An unbounded `.+?` with re.S happily
# spanned entire functions and reported nonsense.
ORDER_BY_RE = re.compile(r"ORDER BY\s+([^;\"\']{0,160}?)\s+LIMIT\s+(\d+|%s)", re.S)


def _sql_text() -> str:
    """The file with Python string plumbing flattened, so an ORDER BY
    split across concatenated literals reads as one clause."""
    text = API.read_text()
    text = re.sub(r'"\s*\n\s*(f?)"', " ", text)      # joined adjacent literals
    text = re.sub(r"#[^\n]*", " ", text)             # comments between them
    return re.sub(r"\s+", " ", text)


def _paged_order_bys() -> list[str]:
    clauses = []
    for match in ORDER_BY_RE.finditer(_sql_text()):
        # LIMIT 1 is exempt: a "give me the latest one" query has no page
        # two, so tied rows cannot repeat or skip across pages. It can
        # still pick an arbitrary winner among ties, which is a different
        # and much smaller problem than corrupting a paged result.
        if match.group(2) == "1":
            continue
        clauses.append(match.group(1).strip())
    return clauses


def test_there_are_paged_queries_to_check():
    # Guards the guard: if the extraction ever stops matching, this test
    # would otherwise pass by finding nothing at all.
    clauses = _paged_order_bys()
    assert len(clauses) >= 4, f"expected several paged queries, found {clauses}"


@pytest.mark.parametrize("clause", _paged_order_bys())
def test_every_paged_query_ends_on_a_unique_column(clause):
    last = clause.split(",")[-1].strip()
    # Strip direction and null-placement modifiers.
    last = re.sub(r"\s+(ASC|DESC)\b", "", last, flags=re.I)
    last = re.sub(r"\s+NULLS\s+(FIRST|LAST)\b", "", last, flags=re.I).strip()
    assert last in UNIQUE_COLUMNS, (
        f"ORDER BY '{clause}' is paired with LIMIT but does not end on a unique column. "
        f"Paging a non-total sort silently repeats and skips rows -- add `id`."
    )
