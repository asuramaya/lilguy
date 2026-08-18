"""Verifies that postings we call "open" can actually still be applied to.

The operator's definition: open means "I can still apply and get it".
That is a claim about the world, and only a request settles it.

Why this is needed at all, given the scraper already closes anything
absent from a source's fresh results: that logic is right, but it trusts
the source. At least one source is not trustworthy. The Muse's API keeps
returning postings its own website has deleted -- all 2,795 of its open
postings had last_seen within two days, while a random sample of six
posted before mid-2025 returned 404 on themuse.com. Six sampled from the
last thirty days returned 200. So the corpus contained roughly 1,176
listings that were presented as open and could not be applied to, about
24% of everything on offer.

Two deliberate conservatisms, both erring toward keeping a posting:

1. Only 404 and 410 close a posting. A 403, a timeout, a 500 or a
   connection error mean "we did not find out", not "it is gone" -- and
   the cost of wrongly closing a live posting (a reader never sees a job
   they could have had) is worse than the cost of leaving a dead one up
   for another cycle.

2. A 200 is accepted as alive even though some ATS platforms serve a
   200 shell for a removed job and only say "no longer available" in
   client-rendered content. Parsing page text to second-guess a 200
   would mean maintaining a per-platform pattern list that silently
   rots. The Muse -- the source this exists for -- returns a real 404,
   so the conservative rule catches the case that motivated the work.
   Where it does not, we are no worse off than before.
"""
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from db import cursor  # noqa: E402

TIMEOUT = 20
PACE_SECONDS = 0.7

# A browser-ish UA: several job sites answer a bare curl UA with a 403
# that means "we don't like you", not "the job is gone" -- which under
# rule 1 above is a deferral, so the practical cost is a queue that
# never converges rather than a wrong close. Better to look ordinary.
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}

GONE_STATUSES = (404, 410)

# Re-check cadence for a posting that IS alive. Old listings are checked
# again sooner because age is what correlates with death -- a posting
# from 2024 that is somehow still live is exactly the kind that will
# quietly disappear.
RECHECK_DAYS_FRESH = 30
RECHECK_DAYS_STALE = 7
STALE_AFTER_DAYS = 180

# Backoff for "we did not find out", capped so a site that blocks us
# cannot monopolise the queue. Same lesson as the description backfill's
# deadlock: what matters is that the queue advances, not the curve.
BACKOFF_HOURS = (6, 24, 72, 168)


def _claim_batch(limit: int) -> list[dict]:
    """Oldest-posted first, because that is where the dead ones are.

    NULLS LAST, not FIRST: a posting with no date at all is not evidence
    of age, and sorting it first hands the front of the queue to the
    least informative rows. Caught live -- the first cycles spent all 36
    of their slots on undated Workday postings before reaching a single
    one of the aged Muse listings this sweep exists for. It also has to
    match the partial index, which already said NULLS LAST.

    `id` is a tiebreaker so the ordering is TOTAL -- without it, rows
    sharing a posted_at_ts can be re-claimed while their neighbours are
    skipped, which is exactly how both the feed's paging and the
    description backfill went wrong.
    """
    with cursor() as cur:
        cur.execute(
            """
            SELECT id, url, company, ats, posted_at_ts
            FROM postings
            WHERE status = 'open'
              AND url IS NOT NULL AND url <> ''
              AND (liveness_next_check_at IS NULL OR liveness_next_check_at <= now())
            ORDER BY liveness_next_check_at NULLS FIRST,
                     posted_at_ts ASC NULLS LAST,
                     id
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def _close(posting_id: str, company: str, status_code: int) -> None:
    with cursor() as cur:
        cur.execute(
            "UPDATE postings SET status = 'closed', closed_at = now(), "
            "liveness_checked_at = now() WHERE id = %s AND status = 'open'",
            (posting_id,),
        )
        if cur.rowcount:
            cur.execute(
                "INSERT INTO events (kind, company, detail) VALUES ('expired', %s, %s)",
                (company, f"HTTP {status_code} on the posting's own URL -- no longer applicable"),
            )


def _alive(posting_id: str) -> None:
    """Record the check and schedule the next one.

    Interval chosen in SQL from the posting's own age so this stays one
    statement and cannot race with a concurrent tick.
    """
    with cursor() as cur:
        cur.execute(
            """
            UPDATE postings
               SET liveness_checked_at = now(),
                   liveness_attempts = 0,
                   liveness_next_check_at = now() + make_interval(days =>
                       CASE WHEN posted_at_ts IS NOT NULL
                             AND posted_at_ts < now() - make_interval(days => %s)
                            THEN %s ELSE %s END)
             WHERE id = %s
            """,
            (STALE_AFTER_DAYS, RECHECK_DAYS_STALE, RECHECK_DAYS_FRESH, posting_id),
        )


def _defer(posting_id: str) -> None:
    with cursor() as cur:
        cur.execute(
            """
            UPDATE postings
               SET liveness_attempts = LEAST(liveness_attempts + 1, 32767),
                   liveness_next_check_at = now() + make_interval(
                       hours => (%s::int[])[LEAST(liveness_attempts + 1, %s)])
             WHERE id = %s
            """,
            (list(BACKOFF_HOURS), len(BACKOFF_HOURS), posting_id),
        )


def run_liveness_sweep(limit: int = 20, pace: float = PACE_SECONDS,
                       session: requests.Session = None) -> dict:
    """Never raises: one unreachable host must not stop the scheduler."""
    rows = _claim_batch(limit)
    if not rows:
        return {"checked": 0, "closed": 0, "alive": 0, "deferred": 0}

    http = session or requests.Session()
    closed = alive = deferred = 0

    for i, row in enumerate(rows):
        # The try wraps ONLY the request. It originally wrapped the
        # database writes too, which meant a schema problem (the events
        # CHECK constraint rejecting 'expired') surfaced as a network
        # deferral -- the sweep cheerfully reported "deferred" while the
        # real fault was local and would never have resolved itself. A
        # catch-all around code that talks to two different systems can
        # only ever tell you about the one you guessed at.
        try:
            resp = http.get(row["url"], headers=UA, timeout=TIMEOUT, allow_redirects=True)
            status = resp.status_code
        except Exception:  # noqa: BLE001 - a network error is "did not find out"
            status = None

        if status in GONE_STATUSES:
            _close(row["id"], row["company"], status)
            closed += 1
        elif status == 200:
            _alive(row["id"])
            alive += 1
        else:
            _defer(row["id"])
            deferred += 1

        if pace and i < len(rows) - 1:
            time.sleep(pace)

    return {"checked": len(rows), "closed": closed, "alive": alive, "deferred": deferred}
