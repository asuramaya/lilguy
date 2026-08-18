"""Lengthens the scrape interval for boards that are valid but empty.

The gap this closes: every self-healing path in this project keys on
FAILURE. consecutive_failures disables a source after five, and
discovery's recheck_disabled_sources gives disabled ones a trial fetch.
A board that answers 200 with an empty list never fails, so it is
scraped at full cadence forever and can never yield anything.

Confirmed live: Koch Industries' Greenhouse board returns a perfectly
valid {"jobs":[],"meta":{"total":0}} and had done so across all 37 runs
since it was added.

The distinction that makes this safe is between an empty BOARD and an
empty RESULT. Nine other sources also had zero open postings at the time
this was written, and every one of them had fetched 78-221 real jobs on
its last run -- they simply have no internships right now, which is
ordinary and must not change their cadence. The signal here is
`fetched_count = 0`: the board itself returned nothing at all.

Backs off rather than disabling, because a company can start posting
again and a disabled source needs a separate path to come back. A source
on a long interval is still checked, just rarely.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from db import cursor  # noqa: E402

# How many consecutive ok-but-empty runs before backing off. High enough
# that a board briefly between postings is not penalised.
EMPTY_RUNS_BEFORE_BACKOFF = 10

# Where an empty board lands. Long enough to stop wasting requests,
# short enough that a company which starts hiring is picked up within a
# day rather than a season.
BACKOFF_INTERVAL_SECONDS = 24 * 3600


def run_empty_board_sweep(cur, runs_threshold: int = EMPTY_RUNS_BEFORE_BACKOFF,
                          interval: int = BACKOFF_INTERVAL_SECONDS) -> int:
    """Returns how many sources were backed off.

    Idempotent: a source already at or beyond the backoff interval is
    left alone, so re-running changes nothing and the count reflects
    real transitions rather than repeated work.
    """
    cur.execute(
        """
        WITH recent AS (
            SELECT source_id,
                   count(*) AS runs,
                   count(*) FILTER (WHERE ok AND fetched_count = 0) AS empty_ok,
                   count(*) FILTER (WHERE NOT ok) AS failures
            FROM (
                SELECT source_id, ok, fetched_count,
                       row_number() OVER (PARTITION BY source_id ORDER BY started_at DESC) AS rn
                FROM scrape_runs
            ) ranked
            WHERE rn <= %s
            GROUP BY source_id
        )
        UPDATE sources s
           SET scrape_interval_seconds = %s,
               next_scrape_at = now() + make_interval(secs => %s)
          FROM recent
         WHERE recent.source_id = s.id
           AND s.status = 'active'
           AND s.scrape_interval_seconds < %s
           -- Every one of the last N runs succeeded AND returned an
           -- empty board. A single failure in the window means we do not
           -- actually know the board is empty, only that we could not
           -- read it -- and that is the failure path's business, not
           -- this one's.
           AND recent.runs >= %s
           AND recent.empty_ok = recent.runs
           AND recent.failures = 0
        """,
        (runs_threshold, interval, interval, interval, runs_threshold),
    )
    return cur.rowcount
