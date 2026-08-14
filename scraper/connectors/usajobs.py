import os

import requests

from .base import Connector, Posting

API = "https://data.usajobs.gov/api/search"


class UsaJobsConnector(Connector):
    """USAJobs.gov's official Search API — the US federal government's own
    job board, covering every federal agency (a real direct-source
    aggregator in the sense that matters here: postings come straight from
    the hiring agencies, not resold/relabeled by a third party). Federal
    Pathways Internship postings live here, and federal agencies run real
    ops/logistics/supply-chain functions (DoD, DoT, GSA, USDA, etc.) —
    directly relevant to this fork's own default focus, not just a token
    "for completeness" addition.

    Confirmed FREE with no cost and no company affiliation required to
    register — a 2-minute email signup at https://developer.usajobs.gov/
    gets an Authorization-Key. Set it as the USAJOBS_API_KEY environment
    variable along with USAJOBS_USER_AGENT (the email you registered with —
    USAJobs requires this as a literal header, not just a courtesy).

    UNVERIFIED END-TO-END, same caveat as adzuna.py and for the same
    reason: this project holds no USAJobs API key, so the exact response
    field names below are built from USAJobs' public API documentation
    (developer.usajobs.gov), not from an actual live 200 response — the
    docs site itself returned 403 to a plain fetch when this was written,
    and the search endpoint correctly 401'd on a fake key rather than
    silently succeeding, which confirms the endpoint/auth model is real,
    just not the exact response shape past that point. Do not add this to
    sources.yaml as a working entry until someone with a real key has run
    it against live data and confirmed the field mapping below — per this
    project's own verify-before-shipping standard (see CONTRIBUTING.md).

    entry needs: {ats: usajobs, keyword: "intern", category_label: "...", max_pages}
    """

    name = "usajobs"

    def fetch(self, entry: dict) -> list[Posting]:
        api_key = os.environ.get("USAJOBS_API_KEY")
        user_agent = os.environ.get("USAJOBS_USER_AGENT")
        if not api_key or not user_agent:
            raise ValueError(
                "USAJOBS_API_KEY / USAJOBS_USER_AGENT not set — register a free key at "
                "https://developer.usajobs.gov/ (USAJOBS_USER_AGENT must be the email you "
                "registered with, USAJobs requires it as a literal header) and export both "
                "as env vars (GitHub Actions: add them as repo secrets)"
            )
        keyword = entry.get("keyword", "intern")

        postings = []
        page = 1
        max_pages = entry.get("max_pages", 10)  # 500/page cap * 10 = 5000 results ceiling per query
        while page <= max_pages:
            resp = requests.get(
                API,
                params={"Keyword": keyword, "ResultsPerPage": 500, "Page": page},
                headers={
                    "Host": "data.usajobs.gov",
                    "User-Agent": user_agent,
                    "Authorization-Key": api_key,
                },
                timeout=20,
            )
            if resp.status_code != 200:
                raise ValueError(f"usajobs query '{keyword}' returned HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            items = (data.get("SearchResult") or {}).get("SearchResultItems", [])
            if not items:
                break

            for item in items:
                job = item.get("MatchedObjectDescriptor", {})
                locations = job.get("PositionLocation") or []
                location = ", ".join(loc.get("LocationName", "") for loc in locations if loc.get("LocationName"))
                postings.append(
                    Posting(
                        id=f"usajobs:{item.get('MatchedObjectId', job.get('PositionID', ''))}",
                        company=job.get("OrganizationName", "US Federal Government"),
                        title=job.get("PositionTitle", ""),
                        location=location or job.get("PositionLocationDisplay", ""),
                        url=job.get("PositionURI", ""),
                        source="usajobs",
                        category=entry.get("category_label", ""),
                        posted_at=job.get("PublicationStartDate"),
                        description_snippet=(job.get("UserArea", {}).get("Details", {}).get("JobSummary")
                                              or job.get("QualificationSummary") or "")[:600],
                    )
                )

            total_pages = int((data.get("SearchResult") or {}).get("SearchResultCountAll", 0))
            if len(items) < 500 or page * 500 >= total_pages:
                break
            page += 1

        return postings
