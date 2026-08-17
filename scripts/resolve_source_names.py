"""Asks each job board what company it belongs to, for sources whose
display name is still a raw URL token.

The categorize-sources workflow identifies companies from the slug plus a
web search, which works for "10xgenomics" and fails for "ag", "jj", "if"
or "mes" -- 32 sources ended up as Unidentified that way. But the boards
themselves know: Greenhouse publishes the board's display name, and
Workday returns a hiringOrganization on every posting. That's an
authoritative answer rather than an inference, so it's worth asking
before falling back to guessing.

    DATABASE_URL=... python3 scripts/resolve_source_names.py [--apply]

Prints proposals by default; --apply writes them. Deliberately two-step:
Workday's hiringOrganization is often a SUBSIDIARY or carries an
internal cost-centre prefix ("4231 Airbus Helicopters, Inc."), so the
output wants a human glance before it becomes the display name on a
company page.
"""
import re
import sys
from collections import Counter
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

import db  # noqa: E402

TIMEOUT = 20
UA = {"User-Agent": "Mozilla/5.0 (internship-feed-bot; name-resolution)", "Accept": "application/json"}

GREENHOUSE_BOARD = "https://boards-api.greenhouse.io/v1/boards/{token}"
WORKDAY_JOBS = "https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
WORKDAY_JOB = "https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"

# Workday tenants routinely prefix the org name with an internal ledger
# code ("4231 Airbus Helicopters, Inc.", "0142 Acme GmbH"). That's their
# accounting, not the company's name.
_LEDGER_PREFIX = re.compile(r"^\s*\d{2,6}[\s\-:]+")


def _clean(name: str) -> str:
    return _LEDGER_PREFIX.sub("", (name or "")).strip()


def resolve_greenhouse(token: str) -> str | None:
    try:
        resp = requests.get(GREENHOUSE_BOARD.format(token=token), headers=UA, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        return _clean(resp.json().get("name"))
    except Exception:  # noqa: BLE001
        return None


def resolve_workday(tenant: str, wd_host: str, site: str, sample: int = 3) -> str | None:
    """Samples several postings and takes the most common hiringOrganization
    -- a single posting can name one subsidiary of a large tenant, so one
    sample would enshrine whichever job happened to be first."""
    try:
        listing = requests.post(
            WORKDAY_JOBS.format(tenant=tenant, wd_host=wd_host, site=site),
            json={"limit": 20, "offset": 0, "searchText": ""},
            headers={"Content-Type": "application/json", **UA}, timeout=TIMEOUT,
        )
        if listing.status_code != 200:
            return None
        postings = listing.json().get("jobPostings", [])[:sample]
        names = []
        for job in postings:
            path = job.get("externalPath", "")
            if not path:
                continue
            detail = requests.get(
                WORKDAY_JOB.format(tenant=tenant, wd_host=wd_host, site=site, path=path),
                headers=UA, timeout=TIMEOUT,
            )
            if detail.status_code != 200:
                continue
            name = _clean(((detail.json().get("hiringOrganization") or {}).get("name")))
            if name:
                names.append(name)
        return Counter(names).most_common(1)[0][0] if names else None
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    apply = "--apply" in sys.argv

    with db.cursor() as cur:
        cur.execute(
            "SELECT id, company, ats, config FROM sources WHERE category = 'Unidentified' ORDER BY company"
        )
        rows = cur.fetchall()

    print(f"{len(rows)} sources to resolve\n")
    proposals = []
    for row in rows:
        cfg = row["config"] or {}
        if row["ats"] == "greenhouse":
            name = resolve_greenhouse(cfg.get("token") or row["company"])
        elif row["ats"] == "workday":
            name = resolve_workday(cfg.get("tenant"), cfg.get("wd_host"), cfg.get("site"))
        else:
            name = None

        status = "->" if name and name.lower() != row["company"].lower() else "  "
        print(f"  {row['company']:26} {status} {name or '(no answer from the board)'}")
        if name and name.lower() != row["company"].lower():
            proposals.append((row["id"], row["company"], name, row["ats"]))

    print(f"\n{len(proposals)} resolvable, {len(rows) - len(proposals)} still unknown")
    if not apply:
        print("(dry run -- pass --apply to write)")
        return 0

    applied = skipped = 0
    for source_id, old, new, ats in proposals:
        with db.cursor() as cur:
            # Same collision guard apply_categorization.py uses: two
            # sources can legitimately resolve to one display name (a
            # company with a general board and a campus one), and that
            # wants a human rather than a silent overwrite.
            cur.execute("SELECT 1 FROM sources WHERE company = %s AND ats = %s AND id != %s",
                        (new, ats, source_id))
            if cur.fetchone():
                print(f"SKIP (would collide): {old} -> {new}")
                skipped += 1
                continue
            cur.execute("SELECT config FROM sources WHERE id = %s", (source_id,))
            config = cur.fetchone()["config"]
            config["company"] = new
            import psycopg2.extras
            cur.execute("UPDATE sources SET company = %s, config = %s WHERE id = %s",
                        (new, psycopg2.extras.Json(config), source_id))
            applied += 1

    print(f"\napplied={applied} skipped={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
