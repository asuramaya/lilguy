"""Populates postings.posted_at_ts / posted_at_approx from the raw
posted_at text already stored on each row.

Run once after deploying the posted_at parsing change; safe to re-run
(it only touches rows whose parsed value would differ from what's
stored), and worth re-running if service/posted_at.py learns a new
format, since the raw provider value is kept precisely so a parser fix
can be replayed against it.

    DATABASE_URL=... python3 scripts/backfill_posted_at.py [--dry-run]

Relative formats ("Posted 2 Days Ago") are anchored to each row's own
first_seen -- the closest record of when that string was actually
fetched -- rather than to now(), which would date every Workday posting
to the moment the backfill happened to run.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

import db  # noqa: E402
from posted_at import parse_posted_at  # noqa: E402

BATCH = 1000


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    with db.cursor() as cur:
        cur.execute(
            "SELECT id, posted_at, first_seen, posted_at_ts, posted_at_approx "
            "FROM postings WHERE posted_at IS NOT NULL"
        )
        rows = cur.fetchall()

    print(f"{len(rows)} postings have a raw posted_at value")

    updates = []
    unparseable = {}
    for row in rows:
        ts, approx = parse_posted_at(row["posted_at"], row["first_seen"])
        if ts is None:
            # Grouped by a short prefix so a genuinely new provider
            # format shows up as a pattern rather than 700 unique lines.
            unparseable[str(row["posted_at"])[:24]] = unparseable.get(str(row["posted_at"])[:24], 0) + 1
            continue
        if row["posted_at_ts"] == ts and row["posted_at_approx"] == approx:
            continue
        updates.append((ts, approx, row["id"]))

    print(f"{len(updates)} rows need updating, {sum(unparseable.values())} unparseable")
    if unparseable:
        print("unparseable samples (prefix -> count):")
        for sample, n in sorted(unparseable.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {n:6d}  {sample!r}")

    if dry_run:
        print("--dry-run: no writes")
        return 0

    for i in range(0, len(updates), BATCH):
        chunk = updates[i:i + BATCH]
        with db.cursor() as cur:
            cur.executemany(
                "UPDATE postings SET posted_at_ts = %s, posted_at_approx = %s WHERE id = %s",
                chunk,
            )
        print(f"  updated {min(i + BATCH, len(updates))}/{len(updates)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
