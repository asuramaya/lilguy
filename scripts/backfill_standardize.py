#!/usr/bin/env python3
"""One-time backfill: apply service/standardize.py's title/location
cleaning to postings already stored before scheduler.py's live ingest
path was wired to do it itself (see that commit's message for the full
history -- clean_display_title/standardize_location existed and were
fully tested, but only ran for the Cloudflare edge export and a one-off
seed script, never for the live scheduler).

Scoped to status IN ('open', 'duplicate') -- closed postings are never
displayed, so cleaning them buys nothing. Only writes rows whose cleaned
value actually differs from what's stored, to avoid needless churn.

dedup_key is deliberately left untouched: it was computed from each
row's RAW title/location at insert time, and every OTHER row's dedup_key
was computed the same way -- recomputing it here from newly-cleaned
values would silently stop matching this row against the rest of the
corpus. Same reasoning as scheduler.py's own ingest path.

Usage:
  ./scripts/backfill_standardize.py                # apply changes
  ./scripts/backfill_standardize.py --dry-run       # report only, write nothing
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

from db import cursor  # noqa: E402
from standardize import clean_display_title, standardize_location  # noqa: E402


def run_backfill(dry_run: bool = False) -> dict:
    changed_title = 0
    changed_location = 0
    unchanged = 0
    total = 0

    with cursor() as cur:
        cur.execute("SELECT id, title, location FROM postings WHERE status IN ('open', 'duplicate')")
        rows = cur.fetchall()

    with cursor() as cur:
        for row in rows:
            total += 1
            new_title = clean_display_title(row["title"]) or row["title"]
            new_location = standardize_location(row["location"])
            title_changed = new_title != row["title"]
            location_changed = new_location != row["location"]
            if not (title_changed or location_changed):
                unchanged += 1
                continue
            if title_changed:
                changed_title += 1
            if location_changed:
                changed_location += 1
            if not dry_run:
                cur.execute(
                    "UPDATE postings SET title = %s, location = %s WHERE id = %s",
                    (new_title, new_location, row["id"]),
                )

    return {
        "total": total,
        "changed_title": changed_title,
        "changed_location": changed_location,
        "unchanged": unchanged,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = parser.parse_args()

    result = run_backfill(dry_run=args.dry_run)
    mode = "DRY RUN -- " if args.dry_run else ""
    print(f"{mode}{result['total']} open/duplicate postings scanned")
    print(f"  title changed:    {result['changed_title']}")
    print(f"  location changed: {result['changed_location']}")
    print(f"  unchanged:        {result['unchanged']}")


if __name__ == "__main__":
    main()
