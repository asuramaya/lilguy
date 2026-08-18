"""Retroactively resolves Workday sources still showing their raw tenant
slug as the company name.

workday_descriptions.py's WorkdayDescriptions.maybe_fix_company applies
the same fix going forward, but only on a posting whose description
hasn't been fetched yet -- every source that finished its description
backfill before that hook existed (which by now is most of them) would
otherwise carry an unresolved name like "ms" forever, since nothing
would ever ask its detail endpoint again. This sweep is what pays down
that existing backlog: one live detail fetch per still-unresolved
source, using whichever open posting it currently has, paced the same
way the description drain is. Safe to run every cycle -- a source this
fixes no longer matches its own WHERE clause, so a second consecutive
run touches nothing.

Deliberately NOT folded into description_backfill.py's queue: that
engine's claim query, backoff and terminal-status tracking are all keyed
on `postings.description IS NULL`, which has nothing to do with what
this is claiming (`sources` rows by their own company/tenant mismatch).
Forcing the two into one shape would be the same mistake as writing one
generic queue and bending unrelated data into it.
"""
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from db import cursor  # noqa: E402
from workday_descriptions import DETAIL_URL, _clean_company_name  # noqa: E402

TIMEOUT = 20
PACE_SECONDS = 0.7
USER_AGENT = "Mozilla/5.0 (internship-feed-bot; company-name-resolution)"


def _claim_batch(limit: int) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """
            SELECT s.id AS source_id, s.config->>'tenant' AS tenant,
                   s.config->>'wd_host' AS wd_host, s.config->>'site' AS site,
                   (SELECT p.id FROM postings p
                     WHERE p.source_id = s.id AND p.status = 'open'
                     ORDER BY p.first_seen DESC LIMIT 1) AS sample_posting_id
            FROM sources s
            WHERE s.ats = 'workday'
              AND s.status IN ('active', 'probation')
              -- The unresolved signal: discovery.py seeds a Common-Crawl
              -- Workday tenant with company=tenant VERBATIM (see its own
              -- comment on that line) -- an exact, case-sensitive match,
              -- not lower(company)=lower(tenant). That distinction is
              -- load-bearing: caught live, ABB's tenant is "abb", and a
              -- case-folded compare flagged "ABB" itself as unresolved
              -- even though it's already the company's correct, properly
              -- cased name (same story for Accenture/accenture,
              -- Expedia/expedia, Unilever/unilever -- any company whose
              -- real name happens to fold onto its own tenant string).
              AND s.company = s.config->>'tenant'
            ORDER BY s.id
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def _update_source_company(source_id: int, company: str) -> None:
    with cursor() as cur:
        cur.execute("UPDATE sources SET company = %s WHERE id = %s", (company, source_id))


def run(limit: int = 5, pace: float = PACE_SECONDS, session: requests.Session = None) -> dict:
    """Never raises: one unreachable tenant must not stop the scheduler."""
    rows = _claim_batch(limit)
    if not rows:
        return {"attempted": 0, "fixed": 0, "skipped": 0}

    http = session or requests.Session()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    fixed = skipped = 0

    for i, row in enumerate(rows):
        pid = row.get("sample_posting_id")
        tenant, wd_host, site = row.get("tenant"), row.get("wd_host"), row.get("site")
        # A source with zero open postings has nothing to fetch a detail
        # page FROM -- not an error, just nothing to do this cycle. It
        # stays claimable (the WHERE clause doesn't change), so it gets
        # picked up again once it has a live posting.
        parts = (pid or "").split(":", 3)
        if not pid or not all((tenant, wd_host, site)) or len(parts) != 4 or not parts[3]:
            skipped += 1
            continue

        url = DETAIL_URL.format(tenant=tenant, wd_host=wd_host, site=site, external_path=parts[3])
        try:
            resp = http.get(url, headers=headers, timeout=TIMEOUT)
            status, payload = resp.status_code, resp
        except Exception:  # noqa: BLE001 - network failures are just skipped, retried next cycle
            status, payload = None, None

        if status == 200:
            try:
                name = _clean_company_name((payload.json().get("hiringOrganization") or {}).get("name") or "")
            except Exception:  # noqa: BLE001 - a malformed body is retried next cycle, not fatal
                name = ""
            if name and name.lower() != tenant.lower():
                _update_source_company(row["source_id"], name)
                fixed += 1
            else:
                skipped += 1
        else:
            skipped += 1

        if pace and i < len(rows) - 1:
            time.sleep(pace)

    return {"attempted": len(rows), "fixed": fixed, "skipped": skipped}
