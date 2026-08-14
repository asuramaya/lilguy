#!/usr/bin/env python3
"""One-time import: sources.yaml -> the `sources` table.

Run once when standing up the DB-backed service against an existing
checkout. Safe to re-run -- upserts on (company, ats), so it won't
duplicate rows if sources.yaml gains new entries later and you run this
again to pick them up. Manually-curated entries from sources.yaml always
get added_by='manual' and status='active' (they're already
verify-before-shipping'd -- see their sources.yaml comments), never
'probation' -- that state is reserved for entries discovery.py itself
proposes.
"""
import sys
from pathlib import Path

import psycopg2.extras
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent))

from db import cursor, init_schema  # noqa: E402

ROOT = Path(__file__).parent.parent
SOURCES_FILE = ROOT / "sources.yaml"

# Per-ats default re-scrape cadence. Aggregators and fast-moving big
# companies churn postings quickly and are cheap per request; slow
# industrials post/close internships rarely and some (Eaton, ITW) cost
# several minutes per fetch at this project's request-pacing settings --
# scraping those hourly would burn most of a scheduler cycle on one
# source for very little freshness benefit.
DEFAULT_INTERVAL_SECONDS = {
    "muse": 3600,
    "adzuna": 3600,
    "greenhouse": 7200,
    "lever": 7200,
    "workday": 21600,
    "oracle_recruiting": 21600,
    "jsonld": 43200,
}


def main() -> int:
    init_schema()
    with SOURCES_FILE.open() as f:
        config = yaml.safe_load(f) or {}
    entries = config.get("sources", [])

    imported = 0
    with cursor() as cur:
        for entry in entries:
            company = entry.get("company")
            ats = entry.get("ats")
            if not company or not ats:
                continue  # commented-out / template entries in sources.yaml parse to None
            category = entry.get("category", "")
            interval = DEFAULT_INTERVAL_SECONDS.get(ats, 21600)
            cur.execute(
                """
                INSERT INTO sources (company, ats, category, config, status, added_by, scrape_interval_seconds)
                VALUES (%s, %s, %s, %s, 'active', 'manual', %s)
                ON CONFLICT (company, ats) DO UPDATE SET
                    config = EXCLUDED.config,
                    category = EXCLUDED.category
                """,
                (company, ats, category, psycopg2.extras.Json(entry), interval),
            )
            imported += 1
    print(f"Imported/updated {imported} source(s) from {SOURCES_FILE.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
