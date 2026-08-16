"""Applies categorize-sources workflow results to `sources`. Run this
against the live DB after Workflow({name: 'categorize-sources', args: [...]})
returns -- save its result to a JSON file and pass that file's path here.

  Workflow({name: "categorize-sources", args: ["acme", "beta", ...]})
  # -> save the returned array as results.json
  DATABASE_URL=... python3 scripts/apply_categorization.py results.json

For each result:
  - Always updates `category` (and config->>'category') -- even
    "Unidentified" is a meaningful signal, distinct from "Uncategorized"
    (never reviewed) vs "Unidentified" (reviewed, genuinely ambiguous).
  - Only renames `company` (and config->>'company') when a real
    company_name was found -- an Unidentified row keeps its raw token as
    company, since we have nothing better to put there.
  - Scoped to added_by='discovery' and the exact current (lowercase slug)
    company value, so this can never touch a manual source even if a
    result's token happened to collide with one.
  - Skips (not overwrites) a rename that would collide with an existing
    (company, ats) pair -- confirmed live this can happen for real
    (companies with two genuinely separate boards, e.g. a general board
    and a campus-recruiting-specific one both matching to the same
    display name) and needs a human look rather than a silent overwrite
    or a crashed batch. Check the SKIP lines this script prints; resolve
    those by hand the way docs/service-architecture.md's "Categorizing
    discovery-promoted sources" section describes.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))
import db  # noqa: E402
import psycopg2.extras  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <results.json>", file=sys.stderr)
        return 1

    with open(sys.argv[1]) as f:
        results = json.load(f)

    updated = 0
    skipped_no_match = 0
    skipped_collision = 0

    # One transaction PER ROW, not one big transaction for all results --
    # a single unique-constraint collision would otherwise roll back
    # every other successful update in the same transaction. db.cursor()
    # opens a fresh connection each call (see db.py), so this is real
    # per-row isolation, not just cosmetic.
    for r in results:
        token = r["token"]
        category = r["category"]
        company_name = r.get("company_name")

        with db.cursor() as cur:
            cur.execute(
                "SELECT id, ats, config FROM sources WHERE company = %s AND added_by = 'discovery'",
                (token,),
            )
            row = cur.fetchone()
            if not row:
                skipped_no_match += 1
                print(f"SKIP (no matching source): {token}")
                continue

            new_company = company_name if company_name else token

            if new_company != token:
                cur.execute(
                    "SELECT 1 FROM sources WHERE company = %s AND ats = %s AND id != %s",
                    (new_company, row["ats"], row["id"]),
                )
                if cur.fetchone():
                    skipped_collision += 1
                    print(f"SKIP (rename would collide): {token} -> {new_company} ({row['ats']})")
                    continue

            config = row["config"]
            config["category"] = category
            config["company"] = new_company

            cur.execute(
                "UPDATE sources SET category = %s, company = %s, config = %s WHERE id = %s",
                (category, new_company, psycopg2.extras.Json(config), row["id"]),
            )
            updated += 1

    print(f"\nupdated={updated} skipped_no_match={skipped_no_match} "
          f"skipped_collision={skipped_collision} total_results={len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
