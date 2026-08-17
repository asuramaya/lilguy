"""Keeps postings' denormalized copies of source fields (company,
category, company_key) from drifting out of sync with sources -- the
systematic fix, not a one-off backfill.

Why this exists instead of just fixing the upsert: the upsert
(scheduler.py's _upsert_postings) only touches postings that appear in a
source's NEXT successful fetch. That misses real cases --

  - closed/duplicate postings never appear in a fresh fetch again (that's
    what closed/duplicate MEANS), so they'd stay stale forever even with
    a correct upsert.
  - a source recategorized while disabled, or while sitting on a long
    scrape_interval_seconds (up to 12h), drifts for hours before its own
    next cycle catches up.
  - any FUTURE field added to sources that should mirror onto postings
    and gets missed from the upsert's SET list the same way category and
    company originally were (confirmed live, 2026-08-17: 101/356 open
    postings still 'Uncategorized' and 5253 postings had a stale company
    after sources itself was fully corrected).

Same shape as dedup.py's run_dedup_sweep on purpose: one cheap,
idempotent, stateless SQL statement, safe to run every scheduler cycle
regardless of whether anything actually changed. Being idempotent is
what makes it self-healing -- it doesn't matter WHY a row went stale
(this bug, a future regression, a hand-edited row), the next sweep just
fixes whatever's still mismatched.
"""


def run_source_sync_sweep(cur) -> int:
    """Re-syncs company/category on every posting from its source row.
    Not scoped to status='open' -- closed and duplicate postings are
    real historical records too, and leaving them stale would mean the
    /duplicates audit view keeps showing pre-correction data forever.
    Returns the number of row-updates applied, NOT distinct rows: a
    posting whose company moved necessarily has a stale company_key too,
    so it is counted by both statements. The number is for logging "the
    sweep did something", and the guarantee that matters is that a
    second consecutive run returns 0."""
    cur.execute(
        """
        UPDATE postings p SET company = s.company, category = s.category
        FROM sources s
        WHERE p.source_id = s.id
          AND (p.company IS DISTINCT FROM s.company OR p.category IS DISTINCT FROM s.category)
        """
    )
    changed = cur.rowcount

    # company_key is derived from company, so it has to be recomputed
    # wherever company just moved -- and for any row that predates the
    # column. Doing it here rather than as a one-off backfill means it is
    # self-healing for the same reason the rest of this sweep is: it
    # doesn't matter WHY a key is missing or stale.
    #
    # The normalization is mirrored in SQL rather than called from
    # dedup.py because this is a set-based UPDATE over the whole table; it
    # must stay in step with compute_company_key, and
    # tests/service/test_source_sync.py asserts the two agree on real
    # inputs precisely so a change to one that isn't mirrored fails loudly.
    cur.execute(
        r"""
        UPDATE postings SET company_key = NULLIF(
            regexp_replace(
              regexp_replace(
                regexp_replace(lower(company),
                  '\y(inc|incorporated|corp|corporation|llc|ltd|limited|co|company|group|holdings|plc)\y\.?', '', 'g'),
                '[^\w\s]', ' ', 'g'),
              '\s', '', 'g'),
            '')
        WHERE company_key IS DISTINCT FROM NULLIF(
            regexp_replace(
              regexp_replace(
                regexp_replace(lower(company),
                  '\y(inc|incorporated|corp|corporation|llc|ltd|limited|co|company|group|holdings|plc)\y\.?', '', 'g'),
                '[^\w\s]', ' ', 'g'),
              '\s', '', 'g'),
            '')
        """
    )
    return changed + cur.rowcount
