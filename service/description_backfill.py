"""Shared machinery for fetching descriptions a connector could not.

Some ATS list endpoints carry no description (Workday, SmartRecruiters),
so those postings are stored with description NULL -- "not fetched yet"
-- and filled in later by a paced background drain. Putting that fetch
on the scrape path instead would make every scrape N+1 requests deep.

This module exists so there is ONE queue implementation rather than one
per platform. Two near-identical backfills would drift, and the first
one cost real production damage while it was alone: it had no attempt
tracking, so a permanently failing row stayed instantly re-claimable at
the head of the queue and starved 465 postings behind it, logging
"0 filled, 0 none-available, 10 deferred" every cycle indefinitely.
Everything that fix taught is built in here from the start:

  - a next_attempt_at with capped exponential backoff, so the queue
    ADVANCES past a row that never succeeds
  - a TOTAL claim ordering (id as the final key), or successive batches
    re-claim one row while skipping its neighbour
  - a try/except around the HTTP call ONLY, so a database or schema
    fault fails loudly instead of masquerading as a network deferral

A platform supplies only what is genuinely platform-specific: which
config fields it needs, how to turn a row into a URL, how to pull text
out of a response, and which status codes mean "gone for good".
"""
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from db import cursor  # noqa: E402

TIMEOUT = 20
PACE_SECONDS = 0.7
BACKOFF_HOURS = (1, 6, 24, 72, 168)

# 403 is here, not just 404/410, because Workday answers 403
# "permission denied" for an UNPUBLISHED job. Verified rather than
# assumed: a stored path 403s consistently while a freshly-listed path
# from the same tenant returns 200 in the same second. A platform that
# genuinely means "forbidden, try later" should override this.
TERMINAL_STATUSES = (403, 404, 410)


class DescriptionSource:
    """What a platform must provide. Subclass and fill in."""

    ats: str = ""
    config_fields: tuple = ()
    user_agent: str = "Mozilla/5.0 (internship-feed-bot; description-fetch)"
    terminal_statuses: tuple = TERMINAL_STATUSES

    def build_url(self, row: dict) -> str | None:
        """Return the detail URL, or None if this row can never resolve.

        None is TERMINAL -- a malformed id will not become well-formed by
        being retried, so it is retired rather than looped on forever.
        """
        raise NotImplementedError

    def extract_text(self, payload: dict) -> str:
        """Display text from a 200 response. '' means the provider
        genuinely publishes none, which is a real answer and is stored as
        such so the row stops being asked about."""
        raise NotImplementedError

    def maybe_fix_company(self, row: dict, payload: dict) -> str | None:
        """Optional: a corrected company NAME for this row's SOURCE, if
        the detail payload reveals one worth using -- None (the default)
        for platforms with nothing extra to offer here, which is most of
        them, since their list endpoint already carries a real company
        name. Applied to `sources.company`, not this one posting: a
        source-level fix is enough for every posting under it, because
        run_source_sync_sweep already re-propagates sources.company onto
        every one of its postings every cycle (see source_sync.py) --
        fixing one row here and leaving the other few hundred stale
        would just be waiting on that same sweep to finish the job
        anyway. A subclass decides FOR ITSELF when a fix is warranted
        (e.g. only when the source's current name still looks
        unresolved); the shared engine just applies whatever it returns.
        """
        return None


def _claim_batch(source: DescriptionSource, limit: int) -> list[dict]:
    fields = "".join(
        f", s.config->>'{name}' AS {name}" for name in source.config_fields
    )
    with cursor() as cur:
        cur.execute(
            f"""
            SELECT p.id, s.id AS source_id, s.company AS source_company{fields}
            FROM postings p
            JOIN sources s ON p.source_id = s.id
            WHERE p.ats = %s
              AND p.status IN ('open', 'duplicate')
              AND p.description IS NULL
              AND (p.description_next_attempt_at IS NULL
                   OR p.description_next_attempt_at <= now())
            ORDER BY p.description_next_attempt_at NULLS FIRST, p.first_seen DESC, p.id
            LIMIT %s
            """,
            (source.ats, limit),
        )
        return cur.fetchall()


def _store(posting_id: str, text: str) -> None:
    with cursor() as cur:
        cur.execute("UPDATE postings SET description = %s WHERE id = %s", (text, posting_id))


def _update_source_company(source_id: int, company: str) -> None:
    with cursor() as cur:
        cur.execute("UPDATE sources SET company = %s WHERE id = %s", (company, source_id))


def _defer(posting_id: str) -> None:
    """Backoff chosen in SQL from the row's own attempt count, so this is
    ONE statement -- reading the count and writing it back would race
    with a concurrent tick and could pin a row to the first rung."""
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


def run(source: DescriptionSource, limit: int = 10, pace: float = PACE_SECONDS,
        session: requests.Session = None) -> dict:
    """Never raises: one unreachable tenant must not stop the scheduler."""
    rows = _claim_batch(source, limit)
    if not rows:
        return {"attempted": 0, "filled": 0, "empty": 0, "deferred": 0}

    http = session or requests.Session()
    headers = {"User-Agent": source.user_agent, "Accept": "application/json"}
    filled = empty = deferred = 0

    for i, row in enumerate(rows):
        url = source.build_url(row)
        if not url:
            _store(row["id"], "")
            empty += 1
            continue

        # Wraps the REQUEST only. Wrapping the database writes too is how
        # a schema fault once surfaced as a network deferral -- a
        # catch-all over two systems can only report the one you guessed.
        try:
            resp = http.get(url, headers=headers, timeout=TIMEOUT)
            status, payload = resp.status_code, resp
        except Exception:  # noqa: BLE001 - network failures are retryable
            status, payload = None, None

        if status in source.terminal_statuses:
            _store(row["id"], "")
            empty += 1
        elif status != 200:
            _defer(row["id"])
            deferred += 1
        else:
            try:
                data = payload.json()
                text = source.extract_text(data)
            except Exception:  # noqa: BLE001 - a malformed body is retryable
                _defer(row["id"])
                deferred += 1
            else:
                _store(row["id"], text)
                if text:
                    filled += 1
                else:
                    empty += 1
                # A bonus side-effect of a detail fetch that was already
                # happening for the description -- never worth failing
                # the whole row over, so a broken hook is swallowed
                # exactly like a malformed description body would be.
                try:
                    fixed_company = source.maybe_fix_company(row, data)
                except Exception:  # noqa: BLE001 - never fatal to the row
                    fixed_company = None
                if fixed_company and row.get("source_id"):
                    _update_source_company(row["source_id"], fixed_company)

        if pace and i < len(rows) - 1:
            time.sleep(pace)

    return {"attempted": len(rows), "filled": filled, "empty": empty, "deferred": deferred}
