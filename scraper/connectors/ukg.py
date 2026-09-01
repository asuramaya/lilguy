import requests

from .base import Connector, Posting
from .util import strip_html, to_display_text

SEARCH_URL = "https://{host}/{tenant}/JobBoard/{board_id}/JobBoardView/LoadSearchResults"


class UkgConnector(Connector):
    """UKG Pro Recruiting (formerly UltiPro) job board. No auth required,
    but the search endpoint needs a specific nested request body -- a
    bare `{}` body confirmed live to silently return `totalCount: 0`
    even on a board with real open postings, which reads exactly like an
    empty board. The real shape, confirmed live against two real
    accounts: {"opportunitySearch": {"Top": N, "Skip": N}}.

    entry needs: {ats: ukg, company, host: "recruiting.ultipro.com" or
                  "recruiting2.ultipro.com", tenant, board_id, category, max_pages?}
    `host`/`tenant`/`board_id` all come straight from the company's own
    board URL: https://<host>/<tenant>/JobBoard/<board_id>

    `tenant` is a semi-readable per-company code (e.g. "UNI1076UNFI") but
    `board_id` is an opaque GUID with no relationship to the company name
    -- same structural gap as oracle_recruiting.py's opaque host, not
    guessable. See candidate_sources.fetch_commoncrawl_ukg_boards for how
    real (tenant, board_id) pairs are found instead of guessed.

    The list response's `JobLocationType` field looks like a structured
    remote/hybrid/onsite signal but its integer values weren't reliably
    decodable from the handful of live examples checked (0 showed up on
    both a plain onsite-looking posting and ones the site's own UI
    labeled "Hybrid") -- left unused rather than guessed; work_arrangement
    still gets set from the location text via work_arrangement.py same as
    every other connector without a trustworthy structured field.
    """

    name = "ukg"

    def fetch(self, entry: dict) -> list[Posting]:
        host = entry.get("host")
        tenant = entry.get("tenant")
        board_id = entry.get("board_id")
        missing = [k for k, v in [("host", host), ("tenant", tenant), ("board_id", board_id)] if not v]
        if missing:
            raise ValueError(f"ukg entry for {entry.get('company')} is missing {missing}")

        url = SEARCH_URL.format(host=host, tenant=tenant, board_id=board_id)
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36", "Content-Type": "application/json"}
        max_pages = entry.get("max_pages", 60)
        page_size = 50
        postings = []
        total = None
        skip = 0

        while total is None or skip < min(total, max_pages * page_size):
            resp = requests.post(
                url, json={"opportunitySearch": {"Top": page_size, "Skip": skip}}, headers=headers, timeout=20
            )
            if resp.status_code != 200:
                raise ValueError(
                    f"ukg host='{host}' tenant='{tenant}' board_id='{board_id}' for {entry.get('company')} "
                    f"returned HTTP {resp.status_code} — check these against the live board URL"
                )
            data = resp.json()
            if "opportunities" not in data:
                raise ValueError(f"unexpected ukg response shape for {entry.get('company')}: {list(data.keys())}")
            if total is None:
                total = data.get("totalCount", 0)

            opportunities = data["opportunities"]
            if not opportunities:
                break
            for job in opportunities:
                job_id = job.get("Id")
                title = (job.get("Title") or "").strip()
                if not job_id or not title:
                    continue

                loc_names = []
                for loc in job.get("Locations") or []:
                    addr = loc.get("Address") or {}
                    parts = [addr.get("City"), (addr.get("State") or {}).get("Code"), (addr.get("Country") or {}).get("Code")]
                    name = ", ".join(p for p in parts if p) or loc.get("LocalizedDescription") or ""
                    if name:
                        loc_names.append(name)
                location = "; ".join(dict.fromkeys(loc_names)) or "Not specified"

                description_raw = job.get("BriefDescription") or ""
                job_url = f"https://{host}/{tenant}/JobBoard/{board_id}/OpportunityDetail?opportunityId={job_id}"

                postings.append(
                    Posting(
                        id=f"ukg:{tenant}:{job_id}",
                        company=entry.get("company", tenant),
                        title=title,
                        location=location,
                        url=job_url,
                        source="ukg",
                        category=entry.get("category", ""),
                        posted_at=job.get("PostedDate"),
                        description_snippet=strip_html(description_raw),
                        description=to_display_text(description_raw),
                    )
                )
            skip += page_size

        return postings
