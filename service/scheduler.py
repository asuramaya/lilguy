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
from dedup import compute_company_key, compute_dedup_key, run_dedup_sweep  # noqa: E402
from posted_at import parse_posted_at  # noqa: E402
from source_sync import run_source_sync_sweep  # noqa: E402
from cycle import parse_cycle  # noqa: E402
from empty_boards import run_empty_board_sweep  # noqa: E402
from work_arrangement import from_location  # noqa: E402
from liveness import run_liveness_sweep  # noqa: E402
from stall import check_for_stall  # noqa: E402
from smartrecruiters_descriptions import fetch_missing_descriptions as fetch_sr_descriptions  # noqa: E402
from workday_descriptions import fetch_missing_descriptions  # noqa: E402
import company_resolution  # noqa: E402

MAX_WORKERS = 6
POLL_INTERVAL_SECONDS = 30
BATCH_SIZE = 20
FAILURE_DISABLE_THRESHOLD = 5
# Bounded per cycle so the one connector needing a second request per
# posting can never crowd out actual scraping.
WORKDAY_DESCRIPTION_BATCH = 10
# Small: this is a one-time backlog (every source it fixes stops
# matching its own claim query), not an ongoing drain like descriptions
# above -- there is no need to hurry it, and a live detail fetch per row
# is exactly the kind of request the description backfill's own pacing
# philosophy applies to.
COMPANY_RESOLUTION_BATCH = 5

# Deliberately small. This makes real requests to real employers' sites
# purely to ask "is this still there", so it must stay a background
# trickle rather than a crawl. At 20 per cycle the ~4.8k open corpus is
# covered in well under a day, which is far faster than postings
# actually die.
LIVENESS_BATCH = 20


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
    #
    # Keyed on source_id, not source_entry -- source_entry is written
    # once at insert time and never corrected, so a later company-name
    # fix (case, typo, rename) orphans every existing posting's
    # source_entry from the source's current company string forever.
    # Confirmed live: 538 sources, 1539 open postings, permanently
    # unclosable this way -- one (pyka) caught red-handed still showing
    # 'open' for a posting that 404s directly and is absent from Lever's
    # own live API. source_id is the stable FK; it never drifts.
    cur.execute(
        """
        UPDATE postings SET status = 'closed', closed_at = %s
        WHERE source_id = %s AND status IN ('open', 'duplicate') AND NOT (id = ANY(%s))
        """,
        (seen_at, source_id, fresh_ids or [""]),
    )
    closed = cur.rowcount

    new_count = 0
    for p in postings:
        dedup_key = compute_dedup_key(p.company, p.title, p.location)
        company_key = compute_company_key(p.company)
        # Anchored on seen_at, not now(), so the stored value means the
        # same thing whenever it's computed -- Workday's "Posted 2 Days
        # Ago" is only meaningful relative to when the page was fetched.
        posted_ts, posted_approx = parse_posted_at(p.posted_at, seen_at)
        cycle_season, cycle_year = parse_cycle(p.title)
        # The connector's own value wins where it has one -- that is the
        # employer answering directly. The location string is the
        # fallback, not an override.
        arrangement = p.work_arrangement or from_location(p.location)
        cur.execute(
            """
            INSERT INTO postings (id, source_id, source_entry, company, title, location, url,
                                   ats, category, job_function, cycle_season, cycle_year,
                                   work_arrangement, posted_at, posted_at_ts, posted_at_approx,
                                   description_snippet, description, status,
                                   dedup_key, company_key, first_seen, last_seen)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                location = EXCLUDED.location,
                url = EXCLUDED.url,
                description_snippet = EXCLUDED.description_snippet,
                -- COALESCE, not a plain overwrite: Workday postings get
                -- their description from a separate per-posting fetch
                -- (its list endpoint has none), so the connector sends
                -- an empty string every cycle. Overwriting would erase
                -- the fetched text on the very next scrape and re-fetch
                -- it forever.
                description = COALESCE(NULLIF(EXCLUDED.description, ''), postings.description),
                dedup_key = EXCLUDED.dedup_key,
                company_key = EXCLUDED.company_key,
                -- category and company both come from this source's own
                -- config (see connectors/*.py: company=entry.get("company",
                -- token), category=entry.get("category", "")), which
                -- apply_categorization.py keeps current -- but neither was
                -- in this SET list originally, so a posting kept whatever
                -- values it happened to get on its first-ever insert,
                -- forever, even once every later scrape fetched the
                -- corrected value. Confirmed live: 101/356 open postings
                -- still showed 'Uncategorized' and 5253 postings had a
                -- stale company after the sources table itself was fully
                -- corrected. service/source_sync.py's periodic sweep is
                -- the safety net for postings this upsert doesn't reach
                -- (closed/duplicate rows, or any future field like this
                -- one that gets missed again) -- this fix is the fast
                -- path, not the only path.
                category = EXCLUDED.category,
                -- Empty string, not NULL, for "this source does not
                -- report that axis" -- matching how `category` has
                -- always spelled it in this table.
                job_function = EXCLUDED.job_function,
                -- Derived from the title, so it must be recomputed
                -- whenever the title changes -- an employer editing
                -- "Summer 2026" to "Summer 2027" is exactly the case
                -- this filter exists to track.
                cycle_season = EXCLUDED.cycle_season,
                cycle_year = EXCLUDED.cycle_year,
                work_arrangement = EXCLUDED.work_arrangement,
                company = EXCLUDED.company,
                posted_at = EXCLUDED.posted_at,
                -- Exact dates just take the new value (successive
                -- scrapes agree, and a provider may legitimately revise
                -- one). APPROXIMATE ones take the EARLIEST estimate
                -- instead, which is not fussiness -- it's the only
                -- correct answer for a saturating bound. Workday's
                -- "Posted 30+ Days Ago" means "posted at or before
                -- now - 30d", an UPPER bound on the date. Re-resolving
                -- it against each new scrape moves that bound forward
                -- forever, so a posting that is genuinely six months
                -- old would report as 30 days old indefinitely -- the
                -- exact staleness this column exists to expose. Keeping
                -- the minimum preserves the tightest bound we ever saw.
                -- (Non-saturating values like "Posted 2 Days Ago" are
                -- self-consistent across scrapes and unaffected.)
                posted_at_ts = CASE
                    WHEN EXCLUDED.posted_at_approx AND postings.posted_at_ts IS NOT NULL
                         AND EXCLUDED.posted_at_ts IS NOT NULL
                    THEN LEAST(postings.posted_at_ts, EXCLUDED.posted_at_ts)
                    ELSE COALESCE(EXCLUDED.posted_at_ts, postings.posted_at_ts)
                END,
                posted_at_approx = EXCLUDED.posted_at_approx,
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
                p.source, p.category, p.job_function, cycle_season, cycle_year,
                arrangement, p.posted_at, posted_ts, posted_approx,
                p.description_snippet,
                # NULLIF so "the connector had no description" stores as
                # NULL (= not fetched yet, eligible) rather than '' (=
                # attempted, provider genuinely has none). Workday's
                # connector always sends empty because its list endpoint
                # carries no description, so inserting '' marked every new
                # Workday posting as already-attempted and made it
                # permanently invisible to workday_descriptions.py.
                # Confirmed live: 3 GE Aerospace postings were retired this
                # way within a minute of deploying.
                p.description or None, dedup_key, company_key, seen_at, seen_at,
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
                # This query only ever selects 'probation'/'active' sources
                # (see run_forever's WHERE clause) -- a disabled source
                # never lands back here until discovery.py's
                # recheck_disabled_sources reinstates it, so this branch
                # fires exactly once per disable, not once per subsequent
                # failed-and-still-disabled poll.
                if new_status == "disabled" and source["status"] != "disabled":
                    cur.execute(
                        "INSERT INTO events (kind, company, detail) VALUES ('disabled', %s, %s)",
                        (source["company"], f"{failures} consecutive failures, last error: {error}"),
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
                with cursor() as cur:
                    synced = run_source_sync_sweep(cur)
                if synced:
                    print(f"  source sync sweep: {synced} posting(s) had company/category re-synced", flush=True)
                # Inside the `if due:` block, unlike liveness: this reads
                # scrape_runs, so it only has anything new to say after a
                # batch has actually written some.
                with cursor() as cur:
                    backed_off = run_empty_board_sweep(cur)
                if backed_off:
                    print(f"  empty-board sweep: {backed_off} source(s) backed off to a long interval",
                          flush=True)

            # Outside the `if due:` block on purpose. Every other connector
            # ships descriptions inside the list response, so only Workday
            # needs this, and its postings sit description-less until it
            # runs -- gating it on "a source was due this cycle" would
            # stall the drain whenever the scrape queue happened to be
            # empty. Small bounded batch, paced, and it only ever touches
            # rows that have never been attempted.
            for label, fetch in (("workday", fetch_missing_descriptions),
                                  ("smartrecruiters", fetch_sr_descriptions)):
                desc = fetch(limit=WORKDAY_DESCRIPTION_BATCH)
                if desc["attempted"]:
                    print(f"  {label} descriptions: {desc['filled']} filled, "
                          f"{desc['empty']} none-available, {desc['deferred']} deferred", flush=True)

            # DISABLED 2026-08-18, same day it shipped: a single job
            # posting's hiringOrganization.name is not a reliable company
            # name for a multinational with many regional/subsidiary
            # postings. Confirmed live and reverted: this sweep replaced
            # good, clean names with subsidiary noise --
            # "3M" -> "CHN 3M Specialty Materials (Shanghai)",
            # "ConocoPhillips" -> "COP AU Op Pty",
            # "Blackstone" -> "70032 Blackstone Europe LLP",
            # "Convatec" -> "1029 Dominican Republic" (doesn't even name
            # the company) -- while it did also produce real
            # improvements ("ms" -> "711 MS Smith Barney, LLC"). The
            # win/loss mix was not something an unattended sweep should
            # have been trusted with, and dozens more well-known
            # companies (sanofi, intel, nvidia, medtronic, target, ...)
            # were still queued behind it when this was caught. Left
            # here, disabled, rather than deleted: the fix-forward half
            # (WorkdayDescriptions.maybe_fix_company) has the exact same
            # flaw and needs the same rethink before either runs again --
            # likely a majority vote across several of a source's
            # postings, or a cross-check against a real company-name
            # source (SEC EDGAR's ticker list, already pulled into
            # discovery.py for an unrelated purpose) instead of trusting
            # any one posting.
            #
            # resolved = company_resolution.run(limit=COMPANY_RESOLUTION_BATCH)
            # if resolved["attempted"]:
            #     print(f"  company name resolution: {resolved['fixed']} fixed, "
            #           f"{resolved['skipped']} skipped", flush=True)

            # Outside `if due:` for the same reason as descriptions: this
            # is not tied to any one source's schedule. It exists because
            # "not in the source's fresh set" only closes a posting when
            # the SOURCE is honest, and The Muse keeps serving listings
            # its own site has deleted.
            # Checked every cycle, including cycles where nothing was
            # due. A stall is precisely the state where nothing happens,
            # so gating the check on work having happened would blind it
            # exactly when it matters.
            with cursor() as cur:
                stall = check_for_stall(cur)
            if stall.get("emitted"):
                print(f"  !! STALLED: no successful scrape since {stall['last_success']}", flush=True)

            live = run_liveness_sweep(limit=LIVENESS_BATCH)
            if live["checked"]:
                print(f"  liveness: {live['checked']} checked, {live['closed']} closed, "
                      f"{live['alive']} still live, {live['deferred']} deferred", flush=True)
            time.sleep(poll_interval)


if __name__ == "__main__":
    try:
        run_forever()
    except KeyboardInterrupt:
        pass
    except Exception:
        traceback.print_exc()
        raise
