#!/usr/bin/env python3
"""Mechanically re-verify a live sample of the corpus, catch drift.

Why this exists ALONGSIDE service/liveness.py's continuous sweep, not
instead of it: that sweep already closes anything it recognizes as gone,
every scheduler cycle, forever. What it can't do is notice when its OWN
recognition logic has gone stale -- a platform changes its close signal,
a newly-added ATS never gets a bespoke check, a regression slips into
check_posting_status. Three real, distinct bugs shipped in one session
this way (Workday treating a block as a closure, Greenhouse's dead jobs
never 404ing at all, SmartRecruiters' rendered page staying a full 200
long after the posting closed) -- each one found by a human/agent
sitting down and sampling the corpus by hand. That doesn't scale and
doesn't repeat itself. This does: same check_posting_status function
service/liveness.py's sweep uses (imported, not re-implemented -- two
copies of this logic drifting apart is exactly how Workday's bug shipped
undetected as long as it did), run on a schedule against a live sample,
reporting a per-ATS discrepancy rate as a trend a human can watch. High
discrepancy rate on some ATS in that trend is the signal that a NEW
bespoke check is needed there, the same way this session found three.

Reads the LIVE corpus (Postgres via DATABASE_URL), not the git-committed
data/all_postings.json snapshot -- auditing a fork's own copy of a
several-days-old export finds none of this. --input FILE remains as an
offline/test fallback only.

Usage:
  ./scripts/audit_liveness.py                       # sample + report, mechanical: also closes confirmed-dead
  ./scripts/audit_liveness.py --no-close             # report only, touch nothing
  ./scripts/audit_liveness.py --sample-size 300
  ./scripts/audit_liveness.py --input data/all_postings.json --limit 50   # offline fallback
"""

import argparse
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR / "service"))

from db import cursor  # noqa: E402
from liveness import GONE_STATUSES, check_posting_status, close_dead_postings  # noqa: E402

DEFAULT_SAMPLE_SIZE = 150
# Above this discrepancy rate for an ATS, print an ALERT line -- the
# same signal that would have flagged Greenhouse (12%) and Workday
# (effectively 100% of its closures, all false) well before either was
# found by hand.
ALERT_THRESHOLD = 0.05


def _sample_from_db(sample_size: int) -> list[dict]:
    """Oldest-open-first per ATS, same bias as liveness.py's own claim
    query and the same reason: that is where the dead ones cluster.
    Stratified across every ATS the corpus actually has, not just the
    biggest one, so a small platform's drift is never invisible just
    because it is outnumbered.
    """
    with cursor() as cur:
        cur.execute("SELECT DISTINCT ats FROM postings WHERE status = 'open' AND ats IS NOT NULL")
        all_ats = [r["ats"] for r in cur.fetchall()]

        rows = []
        for ats in all_ats:
            cur.execute(
                """
                SELECT id, url, company, title, ats, posted_at_ts
                FROM postings
                WHERE status = 'open' AND ats = %s AND url IS NOT NULL AND url <> ''
                ORDER BY posted_at_ts ASC NULLS LAST, id
                LIMIT %s
                """,
                (ats, sample_size),
            )
            rows.extend(cur.fetchall())
        return rows


def _sample_from_file(path: Path, limit: int) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        postings = json.load(f)
    return postings[:limit] if limit > 0 else postings


def run_audit(rows: list[dict], http=None, workers: int = 16) -> dict:
    """The testable core: re-check every row, tally per-ATS results.

    `http` defaults to the `requests` module itself, not a shared
    Session -- module-level requests.get() opens its own connection per
    call, which is what makes this safe to fan out across a
    ThreadPoolExecutor without any of Session's cross-thread caveats.
    Tests inject a fake with the same `.get(url, **kw)` shape instead.

    Returns {"per_ats": {ats: {checked, dead, alive, uncertain}},
             "dead_rows": [(id, company, status), ...]} -- never
    touches the database itself; that split is what makes this callable
    from a test with a scratch Postgres and a fake session, and from
    main() with the real live corpus and real network calls, without
    either path duplicating the other's logic.
    """
    http = http or requests
    per_ats = defaultdict(lambda: {"checked": 0, "dead": 0, "alive": 0, "uncertain": 0})
    dead_rows = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(check_posting_status, row, http): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            ats = row.get("ats") or "unknown"
            status = future.result()
            bucket = per_ats[ats]
            bucket["checked"] += 1
            if status in GONE_STATUSES:
                bucket["dead"] += 1
                dead_rows.append((row["id"], row["company"], status))
            elif status == 200:
                bucket["alive"] += 1
            else:
                bucket["uncertain"] += 1

    return {"per_ats": dict(per_ats), "dead_rows": dead_rows}


def main():
    parser = argparse.ArgumentParser(description="Mechanically re-verify a live sample of the corpus")
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE,
                         help=f"Postings to sample PER ATS from the live DB (default: {DEFAULT_SAMPLE_SIZE})")
    parser.add_argument("--workers", type=int, default=16, help="Concurrency worker threads (default: 16)")
    parser.add_argument("--input", type=str, default=None,
                         help="Offline fallback: read from this JSON file instead of the live DB")
    parser.add_argument("--limit", type=int, default=50, help="Row cap, --input mode only (default: 50)")
    parser.add_argument("--no-close", action="store_true",
                         help="Report only -- do not close confirmed-dead postings")
    parser.add_argument("--json", action="store_true", help="Print the summary as JSON instead of a table")
    args = parser.parse_args()

    offline = args.input is not None
    if offline:
        input_path = ROOT_DIR / args.input
        if not input_path.exists():
            print(f"Error: {input_path} not found")
            sys.exit(1)
        rows = _sample_from_file(input_path, args.limit)
    else:
        rows = _sample_from_db(args.sample_size)

    if not rows:
        print("Nothing open to sample.")
        return

    print(f"Re-verifying {len(rows)} open postings across "
          f"{len(set(r.get('ats') for r in rows))} ATS platform(s), {args.workers} workers...")

    result = run_audit(rows, workers=args.workers)
    per_ats, dead_rows = result["per_ats"], result["dead_rows"]

    total_checked = sum(b["checked"] for b in per_ats.values())
    total_dead = sum(b["dead"] for b in per_ats.values())
    overall_rate = total_dead / total_checked if total_checked else 0

    if args.json:
        print(json.dumps({"checked": total_checked, "dead": total_dead, "per_ats": per_ats}, indent=2))
    else:
        print("\n" + "=" * 72)
        print(f"{'ATS':20} {'checked':>8} {'dead':>6} {'rate':>7}   alert")
        for ats in sorted(per_ats, key=lambda a: -per_ats[a]["dead"] / max(per_ats[a]["checked"], 1)):
            b = per_ats[ats]
            rate = b["dead"] / b["checked"] if b["checked"] else 0
            alert = "  <-- ALERT" if rate > ALERT_THRESHOLD else ""
            print(f"{ats:20} {b['checked']:8} {b['dead']:6} {rate*100:6.1f}%{alert}")
        print("=" * 72)
        print(f"TOTAL: {total_checked} checked, {total_dead} confirmed dead ({overall_rate*100:.1f}%)")

    if offline:
        # An --input run against a static export has no live DB row to
        # close -- reporting only, same as always.
        return

    if dead_rows and not args.no_close:
        close_dead_postings(dead_rows)
        print(f"\nClosed {len(dead_rows)} confirmed-dead postings.")

    alert_ats = [ats for ats, b in per_ats.items()
                 if b["checked"] and b["dead"] / b["checked"] > ALERT_THRESHOLD]
    with cursor() as cur:
        detail = (f"Sampled {total_checked} open postings across {len(per_ats)} ATS platform(s): "
                  f"{total_dead} confirmed dead ({overall_rate*100:.1f}%)"
                  f"{', closed' if dead_rows and not args.no_close else ''}."
                  + (f" ALERT (>{ALERT_THRESHOLD*100:.0f}% dead): {', '.join(alert_ats)}." if alert_ats else ""))
        cur.execute(
            "INSERT INTO events (kind, company, detail) VALUES ('liveness_audit', NULL, %s)",
            (detail,),
        )

    if alert_ats:
        print(f"\nALERT: {', '.join(alert_ats)} exceeded the {ALERT_THRESHOLD*100:.0f}% discrepancy "
              f"threshold -- may need a bespoke check like Workday/Greenhouse/SmartRecruiters got.")
        sys.exit(2)


if __name__ == "__main__":
    main()
