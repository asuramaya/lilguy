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

import re
import psycopg2.extras
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
# NOT (403, 404, 410) despite how that reads -- measured live, Workday's
# per-job CXS detail endpoint 403s on demonstrably still-open postings
# (confirmed by hand against a live tenant, and at scale: CVS Health,
# Enterprise Mobility and TikTok's Workday postings alone racked up
# 100k+ "expired" events between them in a week, while the scheduler's
# own next successful fetch of the SAME source kept re-listing those
# exact postings and reopening them -- liveness closing what the
# scheduler's normal fetch confirms is still there, over and over). The
# CXS detail endpoint appears to apply tighter bot-defense than the
# list endpoint scheduler.py already fetches successfully, so a 403
# here is "we got blocked", not "the job is gone" -- rule 1 in this
# file's own docstring, which the tuple below had silently violated.
WORKDAY_GONE_STATUSES = (404, 410)
GONE_PHRASES = (
    "page doesn't exist",
    "page does not exist",
    "job is no longer available",
    "position is no longer available",
    "job posting has expired",
    "this posting has expired",
    "no longer accepting applications",
    "this job has been closed",
)


def _workday_cxs_url(posting_id: str, url: str) -> str | None:
    parts = (posting_id or "").split(":", 3)
    if len(parts) == 4 and parts[3]:
        tenant, site, path = parts[1], parts[2], parts[3]
        m = re.search(r"\.(wd\d+)\.myworkdayjobs\.com", url or "")
        wd_host = m.group(1) if m else "wd5"
        return f"https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"
    return None

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


def run_liveness_sweep(limit: int = 20, pace: float = PACE_SECONDS,
                       session: requests.Session = None) -> dict:
    """Never raises: one unreachable host must not stop the scheduler."""
    rows = _claim_batch(limit)
    if not rows:
        return {"checked": 0, "closed": 0, "alive": 0, "deferred": 0}

    http = session or requests.Session()
    closed = alive = deferred = 0
    # Outcomes are collected and written ONCE at the end rather than a
    # statement (and, before pooling, a whole connection) per posting.
    # A 20-row sweep was 20 round trips to say the same three things.
    to_close, to_mark_alive, to_defer = [], [], []

    for i, row in enumerate(rows):
        # The try wraps ONLY the request. It originally wrapped the
        # database writes too, which meant a schema problem (the events
        # CHECK constraint rejecting 'expired') surfaced as a network
        # deferral -- the sweep cheerfully reported "deferred" while the
        # real fault was local and would never have resolved itself. A
        # catch-all around code that talks to two different systems can
        # only ever tell you about the one you guessed at.
        try:
            pid = row.get("id") or ""
            p_url = row.get("url") or ""
            is_workday = pid.startswith("workday:") or row.get("ats") == "workday"
            cxs_url = _workday_cxs_url(pid, p_url) if is_workday else None

            if cxs_url:
                cxs_headers = {"User-Agent": UA["User-Agent"], "Accept": "application/json"}
                resp = http.get(cxs_url, headers=cxs_headers, timeout=TIMEOUT)
                status = resp.status_code
                if status in WORKDAY_GONE_STATUSES:
                    status = 404
            else:
                resp = http.get(p_url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
                status = resp.status_code
                resp_text = getattr(resp, "text", "") or ""
                if status == 200 and any(phrase in resp_text.lower() for phrase in GONE_PHRASES):
                    status = 404
                # A 404 status isn't the only way a removed posting answers.
                # Measured live: Greenhouse redirects a dead job's specific
                # URL to its board root with "?error=true" appended -- a
                # 200, no gone-phrase in the (mostly empty, client-rendered)
                # page text, "not found" signaled entirely by the redirect
                # target instead of the status code. Sampled 150 open
                # Greenhouse postings: 18 (12%) redirected this way, all of
                # them landing on error=true with the job id dropped from
                # the path; every OTHER redirect in that sample (a
                # boards.greenhouse.io -> job-boards.greenhouse.io host
                # migration, a trailing-slash cleanup, a bot-challenge
                # interstitial) kept the job id in the final URL. Only
                # trusting this one specific, structured marker -- not
                # "any redirect" -- for the same reason the phrase list
                # above is a fixed set of exact strings rather than a
                # broader "does this look like an error page" guess.
                elif status == 200 and resp.history and "error=true" in resp.url:
                    status = 404
        except Exception:  # noqa: BLE001 - a network error is "did not find out"
            status = None

        if status in GONE_STATUSES:
            to_close.append((row["id"], row["company"], status))
            closed += 1
        elif status == 200:
            to_mark_alive.append(row["id"])
            alive += 1
        else:
            to_defer.append(row["id"])
            deferred += 1

        if pace and i < len(rows) - 1:
            time.sleep(pace)

    _record(to_close, to_mark_alive, to_defer)
    return {"checked": len(rows), "closed": closed, "alive": alive, "deferred": deferred}


def _record(to_close: list, to_mark_alive: list, to_defer: list) -> None:
    """One connection, one transaction, three statements.

    All-or-nothing on purpose: if the process dies mid-sweep the whole
    batch is simply re-claimed next cycle, which is the same outcome as
    never having run. Writing per posting could leave a sweep half
    applied with no record of where it stopped.
    """
    if not (to_close or to_mark_alive or to_defer):
        return
    with cursor() as cur:
        if to_close:
            cur.execute(
                "UPDATE postings SET status = 'closed', closed_at = now(), "
                "liveness_checked_at = now() WHERE id = ANY(%s) AND status = 'open'",
                ([pid for pid, _, _ in to_close],),
            )
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO events (kind, company, detail) VALUES %s",
                [(("expired"), company,
                  f"HTTP {code} on the posting's own URL -- no longer applicable")
                 for _, company, code in to_close],
            )
        if to_mark_alive:
            cur.execute(
                """
                UPDATE postings
                   SET liveness_checked_at = now(),
                       liveness_attempts = 0,
                       liveness_next_check_at = now() + make_interval(days =>
                           CASE WHEN posted_at_ts IS NOT NULL
                                 AND posted_at_ts < now() - make_interval(days => %s)
                                THEN %s ELSE %s END)
                 WHERE id = ANY(%s)
                """,
                (STALE_AFTER_DAYS, RECHECK_DAYS_STALE, RECHECK_DAYS_FRESH, to_mark_alive),
            )
        if to_defer:
            cur.execute(
                """
                UPDATE postings
                   SET liveness_attempts = LEAST(liveness_attempts + 1, 32767),
                       liveness_next_check_at = now() + make_interval(
                           hours => (%s::int[])[LEAST(liveness_attempts + 1, %s)])
                 WHERE id = ANY(%s)
                """,
                (list(BACKOFF_HOURS), len(BACKOFF_HOURS), to_defer),
            )
