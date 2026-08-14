"""The continuous replacement for scrape.py's one-shot batch run.

Runs forever. Each poll cycle: find sources whose next_scrape_at has
passed, dispatch each into a small thread pool (connectors are
synchronous `requests` code -- I/O-bound, so threads parallelize the
actual bottleneck, which is network wait + the deliberate per-request
pacing inside jsonld.py/muse.py, not CPU), and upsert results straight
into Postgres. No code in scraper/connectors/*.py changes at all -- every
connector already takes a plain `entry: dict` (now a DB row's `config`
JSONB column instead of a sources.yaml list item) and returns
list[Posting], which is exactly what this module also expects.

Single scheduler process, on purpose: dispatching a source claims it by
advancing its next_scrape_at BEFORE the fetch runs, using `SELECT ...
FOR UPDATE SKIP LOCKED` so a claim is real even against concurrent
callers (belt-and-suspenders -- discovery.py's recheck loop and this
scheduler could in principle both touch `sources` at once). What's NOT
supported is running multiple *scheduler.py* replicas for throughput --
one process's in-memory ThreadPoolExecutor is the only thing bounding
concurrent fetches, so two replicas would each independently think they
have `MAX_WORKERS` slots free. Horizontal scaling of the scheduler
itself is an explicit non-goal right now -- this project's source count
doesn't need it, and a single container is simpler to self-host, which
was the whole point of this redesign. Revisit if that changes.

A source's failure to fetch is still never treated as "it has zero
postings now" -- the bug this project already found and fixed once (see
docs/sourcing-model.md's "A source failing to fetch is not the same fact
as a source reporting nothing"). Here that guarantee is structural rather
than a special case: _upsert_postings is only ever called on a
successful fetch, so a raised exception simply skips the whole close/
upsert step for that source_entry, leaving its existing postings
untouched.
"""
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent))

from connectors import CONNECTORS  # noqa: E402
from filters import is_internship  # noqa: E402

from db import cursor  # noqa: E402
from dedup import compute_dedup_key, run_dedup_sweep  # noqa: E402

MAX_WORKERS = 6
POLL_INTERVAL_SECONDS = 30
BATCH_SIZE = 20
FAILURE_DISABLE_THRESHOLD = 5


def _now():
    return datetime.now(timezone.utc)


def _claim_due_sources(limit: int) -> list[dict]:
    """Select due sources and immediately push next_scrape_at forward --
    this IS the claim (see module docstring). A source that then fails
    still gets retried on its normal cadence; it isn't re-claimed by a
    concurrent poll cycle while its fetch is still in flight, which
    matters because a single jsonld source can take several minutes
    (ITW: ~580s for ~676 paced requests, confirmed live)."""
    with cursor() as cur:
        cur.execute(
            """
            SELECT id, company, ats, category, config, status,
                   scrape_interval_seconds, consecutive_failures, last_scraped_at
            FROM sources
            WHERE status IN ('probation', 'active') AND next_scrape_at <= now()
            ORDER BY next_scrape_at
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (limit,),
        )
        rows = cur.fetchall()
        if rows:
            ids = [r["id"] for r in rows]
            cur.execute(
                "UPDATE sources SET next_scrape_at = now() + (scrape_interval_seconds || ' seconds')::interval "
                "WHERE id = ANY(%s)",
                (ids,),
            )
        return rows


def _upsert_postings(cur, source_entry: str, source_id: int, postings: list, seen_at: datetime) -> tuple[int, int]:
    """DB analog of store.rebuild(), scoped to ONE source's fresh results.

    Only ever called after a successful fetch, so "not in this source's
    fresh set" reliably means closed/filled -- there is no ambiguity to
    resolve here the way the old global rebuild() had to, because a
    failed fetch never reaches this function at all.
    """
    fresh_ids = [p.id for p in postings]
    # Closes from BOTH 'open' and 'duplicate' -- a posting a dedup sweep
    # previously demoted to 'duplicate' still needs to close when its own
    # source stops returning it, same as an 'open' one would. Leaving
    # 'duplicate' out of this WHERE would strand it in that status
    # forever once its source drops it.
    cur.execute(
        """
        UPDATE postings SET status = 'closed', closed_at = %s
        WHERE source_entry = %s AND status IN ('open', 'duplicate') AND NOT (id = ANY(%s))
        """,
        (seen_at, source_entry, fresh_ids or [""]),
    )
    closed = cur.rowcount

    new_count = 0
    for p in postings:
        dedup_key = compute_dedup_key(p.company, p.title, p.location)
        cur.execute(
            """
            INSERT INTO postings (id, source_id, source_entry, company, title, location, url,
                                   ats, category, posted_at, description_snippet, status,
                                   dedup_key, first_seen, last_seen)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                location = EXCLUDED.location,
                url = EXCLUDED.url,
                description_snippet = EXCLUDED.description_snippet,
                dedup_key = EXCLUDED.dedup_key,
                -- Reopen only if it was 'closed' -- a 'duplicate' status
                -- is service/dedup.py's call, not this upsert's; forcing
                -- it back to 'open' here would just fight the next sweep
                -- and produce status churn every cycle for no reason.
                status = CASE WHEN postings.status = 'closed' THEN 'open' ELSE postings.status END,
                closed_at = CASE WHEN postings.status = 'closed' THEN NULL ELSE postings.closed_at END,
                last_seen = EXCLUDED.last_seen
            """,
            (
                p.id, source_id, source_entry, p.company, p.title, p.location, p.url,
                p.source, p.category, p.posted_at, p.description_snippet, dedup_key, seen_at, seen_at,
            ),
        )
        if cur.rowcount == 1:
            new_count += 1
    return closed, new_count


def run_one(source: dict) -> dict:
    """Fetch one source and reconcile it into Postgres. Never raises --
    a broken source is a result to record, not a reason to kill the
    scheduler loop (same principle as scrape.py's fetch_all())."""
    started_at = _now()
    connector_cls = CONNECTORS.get(source["ats"])
    result = {"company": source["company"], "ok": False}

    try:
        if connector_cls is None:
            raise ValueError(f"unknown ats '{source['ats']}'")
        raw = connector_cls().fetch(source["config"])
        for p in raw:
            p.source_entry = source["company"]
        relevant = [p for p in raw if is_internship(p.title)]

        seen_at = _now()
        with cursor() as cur:
            closed, new_count = _upsert_postings(cur, source["company"], source["id"], relevant, seen_at)

            was_probation_confirmed = source["status"] == "probation" and source["last_scraped_at"] is not None
            new_status = "active" if was_probation_confirmed else source["status"]

            cur.execute(
                """
                UPDATE sources SET
                    status = %s, consecutive_failures = 0,
                    last_scrape_status = 'ok', last_scrape_error = NULL,
                    last_scraped_at = %s
                WHERE id = %s
                """,
                (new_status, seen_at, source["id"]),
            )
            cur.execute(
                """
                INSERT INTO scrape_runs (source_id, started_at, finished_at, fetched_count,
                                          internship_count, ok, error)
                VALUES (%s, %s, %s, %s, %s, true, NULL)
                """,
                (source["id"], started_at, seen_at, len(raw), len(relevant)),
            )

        result.update(ok=True, fetched=len(raw), relevant=len(relevant), new=new_count, closed=closed,
                       promoted=(new_status == "active" and source["status"] == "probation"))
    except Exception as exc:  # noqa: BLE001 - one bad source must not kill the scheduler
        finished_at = _now()
        error = f"{exc}"
        with cursor() as cur:
            failures = source["consecutive_failures"] + 1
            # Logged BEFORE any status change that might delete the
            # `sources` row (the probation-rejection branch below) --
            # scrape_runs.source_id is ON DELETE SET NULL specifically so
            # this row survives that deletion instead of needing to be
            # written after it, which would violate the FK against a
            # source that no longer exists (caught live by
            # tests/service/test_scheduler_reconciliation.py).
            cur.execute(
                """
                INSERT INTO scrape_runs (source_id, started_at, finished_at, fetched_count,
                                          internship_count, ok, error)
                VALUES (%s, %s, %s, NULL, NULL, false, %s)
                """,
                (source["id"], started_at, finished_at, error),
            )
            if source["status"] == "probation":
                # Failed its confirmation fetch -- doesn't get to stay a
                # live source on the strength of one earlier success.
                # Reject it, but keep the evidence rather than silently
                # dropping the row -- see discovery.py's review_status
                # states for what happens to a rejected candidate later.
                cur.execute(
                    """
                    INSERT INTO discovery_candidates (company, ats, config, review_status, evidence, checked_at, next_check_at)
                    VALUES (%s, %s, %s, 'rejected', %s, %s, %s)
                    ON CONFLICT (company) DO UPDATE SET
                        review_status = 'rejected', evidence = EXCLUDED.evidence, checked_at = EXCLUDED.checked_at
                    """,
                    (source["company"], source["ats"], psycopg2.extras.Json(source["config"]),
                     psycopg2.extras.Json({"error": error, "stage": "probation_confirmation"}),
                     finished_at, finished_at + timedelta(days=90)),
                )
                cur.execute("DELETE FROM sources WHERE id = %s", (source["id"],))
            else:
                new_status = "disabled" if failures >= FAILURE_DISABLE_THRESHOLD else source["status"]
                cur.execute(
                    """
                    UPDATE sources SET
                        status = %s, consecutive_failures = %s,
                        last_scrape_status = 'error', last_scrape_error = %s,
                        last_scraped_at = %s
                    WHERE id = %s
                    """,
                    (new_status, failures, error, finished_at, source["id"]),
                )
        result.update(error=error)

    return result


def run_forever(max_workers: int = MAX_WORKERS, poll_interval: int = POLL_INTERVAL_SECONDS,
                 batch_size: int = BATCH_SIZE) -> None:
    print(f"Scheduler starting: {max_workers} workers, polling every {poll_interval}s.", flush=True)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        while True:
            due = _claim_due_sources(batch_size)
            if due:
                print(f"[{_now().isoformat()}] dispatching {len(due)} due source(s)", flush=True)
                futures = [pool.submit(run_one, s) for s in due]
                for f in futures:
                    r = f.result()
                    if r["ok"]:
                        tag = " (PROMOTED to active)" if r.get("promoted") else ""
                        print(f"  {r['company']:30s} ok  fetched={r['fetched']:4d} "
                              f"relevant={r['relevant']:3d} new={r['new']:3d} closed={r['closed']:3d}{tag}", flush=True)
                    else:
                        print(f"  {r['company']:30s} FAILED  {r['error']}", flush=True)
                # Only after a batch that actually wrote something --
                # the sweep is one cheap idempotent SQL statement, but an
                # empty cycle (nothing due) has nothing new to re-rank.
                with cursor() as cur:
                    changed = run_dedup_sweep(cur)
                if changed:
                    print(f"  dedup sweep: {changed} posting(s) changed open/duplicate status", flush=True)
            time.sleep(poll_interval)


if __name__ == "__main__":
    try:
        run_forever()
    except KeyboardInterrupt:
        pass
    except Exception:
        traceback.print_exc()
        raise
