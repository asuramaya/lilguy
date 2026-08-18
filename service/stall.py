"""Notices when the pipeline has stopped doing useful work.

The failure this exists for is a LIVE container that has quietly stopped
producing. A dead container is already caught elsewhere (container-watch
on the host), and that is the easy case -- something is obviously wrong
and something obviously restarts it. The dangerous case is a scheduler
that is running, logging, answering its health check, and scraping
nothing: the feed keeps serving an ageing corpus and looks completely
normal from the outside. Nothing in this service noticed that until now.

Two distinct claims, deliberately kept apart:

  "scraped recently"    -- the loop is turning
  "scraped SUCCESSFULLY" -- the loop is achieving something

Only the second one matters. A scheduler cheerfully failing every source
every cycle is stalled in every sense a reader cares about, while
looking maximally healthy to anything counting activity.
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from db import cursor  # noqa: E402

# Sources are scraped continuously and none currently goes unscraped for
# even a day, so a few hours of NO successful run anywhere is already far
# outside normal. Long enough not to fire on a slow patch or a restart,
# short enough to be worth knowing the same day.
STALL_AFTER = timedelta(hours=3)


def check_for_stall(cur, stall_after: timedelta = STALL_AFTER) -> dict:
    """Emits a 'stalled' event if warranted. Returns what it found.

    Emits at most ONCE per stall episode. The boundary is the last
    SUCCESS: an existing 'stalled' event newer than the last successful
    run means this same episode has already been reported, and repeating
    it every cycle would turn the events panel into a stuck alarm that
    nobody reads. Once the pipeline recovers, the next success advances
    past the old event and a future stall reports again.
    """
    cur.execute("SELECT max(started_at) AS at FROM scrape_runs WHERE ok")
    last_success = cur.fetchone()["at"]

    cur.execute("SELECT max(created_at) AS at FROM events WHERE kind = 'stalled'")
    last_alert = cur.fetchone()["at"]

    # No successful run EVER is a fresh deployment, not a stall. Saying
    # otherwise would greet every new install with a fault report.
    if last_success is None:
        return {"stalled": False, "reason": "no successful run yet", "emitted": False}

    cur.execute("SELECT now() - %s > %s AS overdue, now() AS now", (last_success, stall_after))
    row = cur.fetchone()
    if not row["overdue"]:
        return {"stalled": False, "last_success": last_success, "emitted": False}

    already_reported = last_alert is not None and last_alert > last_success
    if not already_reported:
        hours = (row["now"] - last_success).total_seconds() / 3600
        cur.execute(
            "INSERT INTO events (kind, company, detail) VALUES ('stalled', NULL, %s)",
            # Trailing comma matters: without it these parentheses are
            # string concatenation, not a one-element tuple, and psycopg2
            # gets a bare str where it wants a sequence.
            (f"No source has been scraped successfully in {hours:.1f} hours — "
             "the feed is serving an ageing corpus",),
        )

    return {"stalled": True, "last_success": last_success, "emitted": not already_reported}
