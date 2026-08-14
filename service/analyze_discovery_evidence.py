#!/usr/bin/env python3
"""Turns verify.py's threshold-tuning question from a guess into a
measurement, once the discovery loop has actually run for a while.

verify.py's gate (MIN_INTERNSHIP_POSTINGS=1, MIN_DISTINCT_TITLES=2,
NAME_MATCH_THRESHOLD=0.5) was reasoned from what already went wrong by
hand this session (a placeholder page, a tenant-string collision), not
measured against real discovery results -- there weren't any yet. This
script doesn't change anything; it reads back the evidence every
verify_trial_fetch() call already writes into discovery_candidates.
evidence (see verify.py's own docstring -- that JSONB blob was always
meant to make a rejection legible later, not just at the moment it
happened) and buckets it by outcome, so a human deciding whether e.g.
NAME_MATCH_THRESHOLD is too loose/strict can look at the actual
distribution of name_similarity scores across promoted vs rejected
candidates instead of guessing again.

Usage:
  python service/analyze_discovery_evidence.py
"""
import sys
from collections import Counter
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, str(Path(__file__).parent))

from db import cursor  # noqa: E402


def _numeric_stats(values: list) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    return {"n": len(values), "min": round(min(values), 2), "max": round(max(values), 2),
            "mean": round(mean(values), 2), "median": round(median(values), 2)}


def summarize(rows: list[dict]) -> dict:
    """rows: [{"review_status": ..., "evidence": {...} | None, "ats": ...}, ...]
    -- pure function, no DB, so this is unit-testable on synthetic data
    without needing a real discovery run first.
    """
    by_status = Counter(r["review_status"] for r in rows)

    rejected_reasons = Counter()
    name_similarity_by_outcome = {"promoted": [], "rejected": []}
    intern_count_by_outcome = {"promoted": [], "rejected": []}

    for r in rows:
        status = r["review_status"]
        evidence = r.get("evidence") or {}
        if status == "rejected":
            rejected_reasons[evidence.get("reason", "(no reason recorded)")] += 1
        if status in ("promoted", "rejected"):
            name_similarity_by_outcome[status].append(evidence.get("name_similarity"))
            intern_count_by_outcome[status].append(evidence.get("intern_count"))

    return {
        "counts_by_review_status": dict(by_status),
        "rejected_reasons": dict(rejected_reasons),
        "name_similarity": {k: _numeric_stats(v) for k, v in name_similarity_by_outcome.items()},
        "intern_count": {k: _numeric_stats(v) for k, v in intern_count_by_outcome.items()},
    }


def _fetch_rows() -> list[dict]:
    with cursor() as cur:
        cur.execute("SELECT review_status, evidence, ats FROM discovery_candidates WHERE checked_at IS NOT NULL")
        return cur.fetchall()


def _fetch_promotion_confirmation_rate() -> dict:
    """How many discovery-promoted companies actually made it to 'active'
    (confirmed twice) vs are still 'probation' vs got rejected on their
    confirmation attempt (removed from `sources`, see scheduler.py) --
    joins discovery_candidates against the CURRENT sources table, since
    review_status='promoted' only ever means "passed the FIRST gate," not
    the full two-strike story.
    """
    with cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE s.status = 'active') AS confirmed_active,
                COUNT(*) FILTER (WHERE s.status = 'probation') AS still_on_probation,
                COUNT(*) FILTER (WHERE s.id IS NULL) AS no_longer_in_sources
            FROM discovery_candidates dc
            LEFT JOIN sources s ON s.company = dc.company AND s.ats = dc.ats AND s.added_by = 'discovery'
            WHERE dc.review_status = 'promoted'
            """
        )
        return cur.fetchone()


def main() -> int:
    rows = _fetch_rows()
    if not rows:
        print("No checked discovery_candidates yet -- run the discovery loop first.")
        return 0

    result = summarize(rows)
    print(f"Checked {len(rows)} candidate(s) so far.\n")

    print("By review_status:")
    for status, count in sorted(result["counts_by_review_status"].items()):
        print(f"  {status:12s} {count}")

    if result["rejected_reasons"]:
        print("\nRejection reasons:")
        for reason, count in sorted(result["rejected_reasons"].items(), key=lambda kv: -kv[1]):
            print(f"  {count:4d}  {reason}")

    print("\nname_similarity (verify.py's NAME_MATCH_THRESHOLD is 0.5):")
    for outcome, stats in result["name_similarity"].items():
        print(f"  {outcome:10s} {stats}")

    print("\nintern_count found in trial fetch:")
    for outcome, stats in result["intern_count"].items():
        print(f"  {outcome:10s} {stats}")

    confirmation = _fetch_promotion_confirmation_rate()
    if confirmation and (confirmation["confirmed_active"] or confirmation["still_on_probation"]
                          or confirmation["no_longer_in_sources"]):
        print("\nOf candidates that passed the FIRST gate ('promoted' -> probation):")
        print(f"  confirmed to active:     {confirmation['confirmed_active']}")
        print(f"  still on probation:      {confirmation['still_on_probation']}")
        print(f"  failed confirmation:     {confirmation['no_longer_in_sources']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
