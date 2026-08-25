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

MAJORITY VOTE, not a single posting's say-so -- this sweep originally
trusted whichever ONE open posting a source happened to have, and was
disabled the same day it shipped (2026-08-18): confirmed live, a
multinational's regional/subsidiary postings each report their OWN
legal entity as hiringOrganization.name, not the parent company, so a
single sample replaced good, clean names with subsidiary noise ("3M" ->
"CHN 3M Specialty Materials (Shanghai)", "ConocoPhillips" -> "COP AU Op
Pty", "Blackstone" -> "70032 Blackstone Europe LLP") about as often as
it produced a real improvement ("ms" -> "711 MS Smith Barney, LLC").
Sampling several of a source's open postings and requiring a genuine
majority (not just a plurality) among the names that actually answer
fixes this: a real parent company's name recurring across most of its
own postings outvotes any one subsidiary's noise, and a source whose
postings disagree with no majority is left alone rather than guessed at
-- unresolved-but-honest beats resolved-but-wrong, which is exactly the
lesson the disabled version's postmortem drew.

workday_descriptions.py's WorkdayDescriptions.maybe_fix_company stays
disabled deliberately, not fixed the same way: it fires opportunistically
off ONE posting's own description fetch and has no natural way to see a
source's other postings without restructuring that hook's whole per-
posting design. This sweep is now the single, authoritative resolution
path -- simpler than making two independent mechanisms both trustworthy.

Deliberately NOT folded into description_backfill.py's queue: that
engine's claim query, backoff and terminal-status tracking are all keyed
on `postings.description IS NULL`, which has nothing to do with what
this is claiming (`sources` rows by their own company/tenant mismatch).
Forcing the two into one shape would be the same mistake as writing one
generic queue and bending unrelated data into it.
"""
import sys
import time
from collections import Counter
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from db import cursor  # noqa: E402
from workday_descriptions import DETAIL_URL, _clean_company_name  # noqa: E402

TIMEOUT = 20
PACE_SECONDS = 0.7
USER_AGENT = "Mozilla/5.0 (internship-feed-bot; company-name-resolution)"

# How many of a source's open postings to sample before trusting a name.
# Higher would strengthen the majority-vote signal further but costs one
# more live fetch per source per point -- 5 was enough to separate the
# confirmed-live good and bad cases without adding a full extra request
# round on top of what description_backfill.py's pacing already costs.
SAMPLE_SIZE = 5
# A genuine majority (strictly more than half of the postings that
# actually answered), not a plurality -- three subsidiaries agreeing
# with each other should not outvote one that happens to be the real
# parent name if there's no clear majority either way. Below this, the
# source is left unresolved rather than guessed at.
MIN_SAMPLES = 2


def _claim_batch(limit: int) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """
            SELECT s.id AS source_id, s.config->>'tenant' AS tenant,
                   s.config->>'wd_host' AS wd_host, s.config->>'site' AS site,
                   COALESCE(
                       (SELECT array_agg(p.id ORDER BY p.first_seen DESC)
                        FROM (SELECT id, first_seen FROM postings
                              WHERE source_id = s.id AND status = 'open'
                              ORDER BY first_seen DESC LIMIT %s) p),
                       ARRAY[]::text[]
                   ) AS sample_posting_ids
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
            (SAMPLE_SIZE, limit),
        )
        return cur.fetchall()


def _update_source_company(source_id: int, company: str) -> None:
    with cursor() as cur:
        cur.execute("UPDATE sources SET company = %s WHERE id = %s", (company, source_id))


def _fetch_name(http, headers, tenant, wd_host, site, pid) -> str | None:
    """One posting's own hiringOrganization.name, cleaned. None on any
    failure -- a single bad fetch is a missing vote, not a fatal error."""
    parts = (pid or "").split(":", 3)
    if not pid or not all((tenant, wd_host, site)) or len(parts) != 4 or not parts[3]:
        return None
    url = DETAIL_URL.format(tenant=tenant, wd_host=wd_host, site=site, external_path=parts[3])
    try:
        resp = http.get(url, headers=headers, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        name = _clean_company_name((resp.json().get("hiringOrganization") or {}).get("name") or "")
    except Exception:  # noqa: BLE001 - a bad fetch/body is a missing vote, retried next cycle
        return None
    return name or None


def run(limit: int = 5, pace: float = PACE_SECONDS, session: requests.Session = None) -> dict:
    """Never raises: one unreachable tenant must not stop the scheduler."""
    rows = _claim_batch(limit)
    if not rows:
        return {"attempted": 0, "fixed": 0, "skipped": 0}

    http = session or requests.Session()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    fixed = skipped = 0
    first_request = True

    for row in rows:
        tenant, wd_host, site = row.get("tenant"), row.get("wd_host"), row.get("site")
        pids = row.get("sample_posting_ids") or []
        # A source with zero open postings has nothing to sample from --
        # not an error, just nothing to do this cycle. It stays claimable
        # (the WHERE clause doesn't change), so it gets picked up again
        # once it has a live posting.
        if not pids:
            skipped += 1
            continue

        names = []
        for pid in pids:
            if not first_request and pace:
                time.sleep(pace)
            first_request = False
            name = _fetch_name(http, headers, tenant, wd_host, site, pid)
            if name and name.lower() != (tenant or "").lower():
                names.append(name)

        if len(names) < MIN_SAMPLES:
            skipped += 1
            continue

        top_name, top_count = Counter(names).most_common(1)[0]
        if top_count > len(names) / 2:
            _update_source_company(row["source_id"], top_name)
            fixed += 1
        else:
            skipped += 1

    return {"attempted": len(rows), "fixed": fixed, "skipped": skipped}
