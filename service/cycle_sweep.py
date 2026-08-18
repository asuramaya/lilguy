"""Fills cycle_season/cycle_year on postings the upsert never revisits.

The upsert is the fast path: every re-scrape recomputes the cycle from
the title. But a posting whose source has stopped returning it is closed
and never upserted again, and rows that predate this column would
otherwise sit blank forever. Same belt-and-braces shape as
service/source_sync.py, and for the same reason -- a derived field needs
both a write path and a sweep, or it drifts.

Deliberately Python rather than a SQL regex: service/cycle.py is the one
definition of what a cycle string means, and a second implementation in
SQL would be obliged to agree with it forever.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cycle import parse_cycle  # noqa: E402
from db import cursor  # noqa: E402
from work_arrangement import from_location  # noqa: E402

BATCH = 5000


def run_cycle_sweep(cur, batch: int = BATCH) -> int:
    """Returns how many postings had their cycle corrected.

    Recomputes for EVERY row rather than only blank ones: the parser can
    change, and a row parsed by an older version must be allowed to
    improve. Only rows whose value actually differs are written, so a
    steady state costs a read and no writes.
    """
    cur.execute(
        "SELECT id, title, location, ats, cycle_season, cycle_year, work_arrangement "
        "FROM postings ORDER BY id LIMIT %s",
        (batch,),
    )
    rows = cur.fetchall()

    changed = []
    for row in rows:
        season, year = parse_cycle(row["title"])
        # Location-derived only. A connector's OWN structured value is
        # set at upsert time and must not be second-guessed here -- this
        # sweep cannot see it, so it only ever fills a blank rather than
        # overwriting what the employer said.
        arrangement = row["work_arrangement"] or from_location(row["location"])
        if (season != (row["cycle_season"] or "")
                or year != row["cycle_year"]
                or arrangement != (row["work_arrangement"] or "")):
            changed.append((season, year, arrangement, row["id"]))

    for season, year, arrangement, posting_id in changed:
        cur.execute(
            "UPDATE postings SET cycle_season = %s, cycle_year = %s, work_arrangement = %s "
            "WHERE id = %s",
            (season, year, arrangement, posting_id),
        )
    return len(changed)


def main() -> int:
    total = 0
    with cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM postings")
        print(f"{cur.fetchone()['n']} postings to consider")
        changed = run_cycle_sweep(cur, batch=10_000_000)
        total += changed
    print(f"updated {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
