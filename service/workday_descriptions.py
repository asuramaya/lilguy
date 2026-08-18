"""Backfills descriptions for Workday postings, which are the only ones
that need a second request to get any.

Every other connector hands back the full description inside the list
response we already fetch -- Greenhouse's `content`, Muse's `contents`,
Lever's `descriptionPlain`, JSON-LD's `description`, Oracle's three
composable fields. Workday's job-search endpoint carries none: the
closest thing, `bulletFields[0]`, is a requisition-ID stub. So the text
has to come from Workday's per-posting CXS endpoint, confirmed live:

    GET https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{external_path}
    -> {"jobPostingInfo": {"jobDescription": "<html>", ...}, ...}

No auth, ~11KB per posting, 9k characters of description in the sample.

WHY THIS IS AFFORDABLE. The cost is bounded by NEW postings, not by
total ones, because a description is fetched exactly once: rows are
picked up only while `description IS NULL`, and the scheduler's upsert
COALESCEs rather than overwrites, so a later scrape can't blank what was
fetched and cause a re-fetch. After the initial drain the steady-state
cost is however many new Workday postings appeared that cycle, which is
normally a handful.

THE THREE-STATE COLUMN is what makes "once" enforceable:
    NULL  -- never attempted, eligible
    ''    -- attempted, and the provider genuinely had nothing (or the
             posting is gone); do NOT keep retrying
    text  -- got it
A transient failure (timeout, 5xx, connection error) deliberately leaves
NULL so it retries later, while a definitive 404/410 writes '' so a
posting that will never resolve doesn't get re-requested every cycle
forever. Confusing those two is how a backfill turns into a permanent
background load against someone else's servers.
"""
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent))

from connectors.util import to_display_text  # noqa: E402

from db import cursor  # noqa: E402

DETAIL_URL = "https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{external_path}"
TIMEOUT = 20
# Paced like every other connector in this project. In practice the load
# per host is far gentler than this suggests, because a batch is drawn
# from whichever postings are missing text and those spread across many
# different tenants rather than hammering one.
PACE_SECONDS = 0.7
UA = {"User-Agent": "Mozilla/5.0 (internship-feed-bot; description-fetch)", "Accept": "application/json"}

# Workday answers 403 "errorCode: S22 / permission denied" for a job that
# has been UNPUBLISHED -- not 404, which is what you would expect and
# what this code originally assumed. Verified rather than guessed: a
# stored path returns 403 consistently while a freshly-listed path from
# the SAME tenant and site returns 200 in the same second, from two
# different hosts and two different User-Agents, with the list endpoint
# also answering 200 throughout. So it is the job that is gone, not us
# being rate-limited or blocked.
#
# Treating 403 as transient is what let dead rows retry forever.
TERMINAL_STATUSES = (403, 404, 410)

# Exponential, capped. The cap matters more than the curve: a row that
# will never succeed must still come back around occasionally (a tenant
# can be genuinely down for a week), but must never again be able to
# monopolise the queue.
BACKOFF_HOURS = (1, 6, 24, 72, 168)


def _external_path(posting_id: str) -> str:
    """Posting ids are built as f"workday:{tenant}:{site}:{external_path}"
    (see scraper/connectors/workday.py), and external_path itself contains
    slashes but never colons, so splitting on the first three colons
    recovers it exactly."""
    parts = posting_id.split(":", 3)
    return parts[3] if len(parts) == 4 else ""


def _claim_batch(limit: int) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """
            SELECT p.id, s.config->>'tenant' AS tenant, s.config->>'wd_host' AS wd_host,
                   s.config->>'site' AS site
            FROM postings p
            JOIN sources s ON p.source_id = s.id
            WHERE p.ats = 'workday' AND p.status IN ('open', 'duplicate')
              AND p.description IS NULL
              AND s.config->>'tenant' IS NOT NULL
              AND s.config->>'wd_host' IS NOT NULL
              AND s.config->>'site' IS NOT NULL
              AND (p.description_next_attempt_at IS NULL
                   OR p.description_next_attempt_at <= now())
            -- `id` is a tiebreaker, not decoration: rows inserted by one
            -- scrape share a first_seen to the microsecond, so without it
            -- the claim order is not TOTAL and successive batches can
            -- re-claim the same row while skipping its neighbour. Same
            -- shape as the bug that broke feed paging.
            ORDER BY p.description_next_attempt_at NULLS FIRST, p.first_seen DESC, p.id
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def _defer(posting_id: str) -> None:
    """Push a row that failed transiently into the future.

    This is the whole fix for the deadlock. Without it a failing row
    stays instantly re-claimable and the queue cannot advance past it,
    no matter how the failure is classified.

    Backoff is chosen in SQL from the row's own current attempt count so
    this is ONE statement -- reading the count and then writing it back
    would race with a concurrent scheduler tick and could reset a row's
    backoff to the first rung forever. Postgres arrays are 1-indexed, so
    a row at 0 attempts gets BACKOFF_HOURS[1] = 1 hour.
    """
    with cursor() as cur:
        cur.execute(
            """
            UPDATE postings
               SET description_attempts = LEAST(description_attempts + 1, 32767),
                   description_next_attempt_at = now() + make_interval(
                       hours => (%s::int[])[LEAST(description_attempts + 1, %s)])
             WHERE id = %s
            """,
            (list(BACKOFF_HOURS), len(BACKOFF_HOURS), posting_id),
        )


def _store(posting_id: str, text: str) -> None:
    with cursor() as cur:
        cur.execute("UPDATE postings SET description = %s WHERE id = %s", (text, posting_id))


def fetch_missing_descriptions(limit: int = 10, pace: float = PACE_SECONDS,
                                session: requests.Session = None) -> dict:
    """Returns a small summary for the scheduler's log. Never raises: one
    unreachable tenant must not take down the loop that calls this."""
    rows = _claim_batch(limit)
    if not rows:
        return {"attempted": 0, "filled": 0, "empty": 0, "deferred": 0}

    http = session or requests.Session()
    filled = empty = deferred = 0

    for i, row in enumerate(rows):
        external_path = _external_path(row["id"])
        if not external_path:
            # Malformed id -- it will never resolve, so retiring it is
            # correct rather than retrying it forever.
            _store(row["id"], "")
            empty += 1
            continue

        url = DETAIL_URL.format(tenant=row["tenant"], wd_host=row["wd_host"],
                                 site=row["site"], external_path=external_path)
        try:
            resp = http.get(url, headers=UA, timeout=TIMEOUT)
            if resp.status_code in TERMINAL_STATUSES:
                _store(row["id"], "")   # definitively gone; stop asking
                empty += 1
            elif resp.status_code != 200:
                _defer(row["id"])       # transient; back off and retry later
                deferred += 1
            else:
                description = (resp.json().get("jobPostingInfo") or {}).get("jobDescription") or ""
                text = to_display_text(description)
                _store(row["id"], text)
                if text:
                    filled += 1
                else:
                    empty += 1
        except Exception:  # noqa: BLE001 - network/JSON/DB, all retryable
            _defer(row["id"])
            deferred += 1

        if pace and i < len(rows) - 1:
            time.sleep(pace)

    return {"attempted": len(rows), "filled": filled, "empty": empty, "deferred": deferred}
