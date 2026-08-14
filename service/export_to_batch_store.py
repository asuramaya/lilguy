#!/usr/bin/env python3
"""Exports the live service's current postings into the EXACT same JSON
shape scraper/store.py already produces for data/all_postings.json --
so build_feed.py, user_filter.py, and FEED.md keep working completely
unchanged against it. Proves the two pipelines (git-committed batch,
Postgres-backed live service) are reconcilable by construction, without
deciding WHEN this should run.

That "when" is deliberately left open here -- run it by hand, wire it
into a cron container, whatever -- because it's a deployment-cadence
question, not a data-format one, and this project isn't deciding
deployment yet (see the standing decision: hyper-docker + a cupid
handoff over Osiris mail, once we're actually ready for that phase).
What belongs in THIS layer is just: does a live Postgres postings table
turn into a byte-for-byte-compatible data/all_postings.json? Yes,
proven by test_export_to_batch_store.py.

Usage:
  python service/export_to_batch_store.py > data/all_postings.json
  python service/export_to_batch_store.py --out data/all_postings.json
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from db import cursor  # noqa: E402


def fetch_open_postings_as_dicts() -> list[dict]:
    """Same field SET and same field NAMES as Posting.to_dict() + store.
    rebuild()'s added "first_seen" -- scraper/store.py's own consumers
    (build_feed.py, user_filter.py) index by these exact keys and don't
    know or care that the data came from Postgres instead of a fresh
    scrape. `duplicate` and `closed` postings are excluded, matching the
    batch pipeline's own semantics: data/all_postings.json only ever
    holds currently-open postings, dedup'd the same way api.py's /feed
    already dedup's them (a `duplicate`-status row isn't "gone", just
    not the canonical one to show).
    """
    with cursor() as cur:
        cur.execute(
            """
            SELECT id, company, title, location, url, ats AS source, category,
                   posted_at, description_snippet, source_entry, first_seen
            FROM postings
            WHERE status = 'open'
            ORDER BY first_seen DESC
            """
        )
        rows = cur.fetchall()
    return [
        {
            "id": r["id"],
            "company": r["company"],
            "title": r["title"],
            "location": r["location"],
            "url": r["url"],
            "source": r["source"],
            "category": r["category"],
            "posted_at": r["posted_at"],
            "description_snippet": r["description_snippet"],
            "source_entry": r["source_entry"],
            "first_seen": r["first_seen"].isoformat(),
        }
        for r in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, help="Write to this file instead of stdout")
    args = parser.parse_args()

    postings = fetch_open_postings_as_dicts()
    output = json.dumps(postings, indent=2) + "\n"

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(output)
        print(f"Wrote {len(postings)} posting(s) to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
