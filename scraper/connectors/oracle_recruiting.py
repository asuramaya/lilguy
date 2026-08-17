import requests

from .base import Connector, Posting
from .util import strip_html, to_display_text

API = "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"


class OracleRecruitingConnector(Connector):
    """Oracle Recruiting Cloud (aka Oracle Fusion Recruiting / Taleo's
    successor) — a real, distinct fourth ATS platform this project had
    zero coverage of until now. No connector existed for it because it
    has no public sitemap the jsonld.py connector could find (confirmed
    on Honeywell, which runs it — see sources.yaml/docs history) and its
    job data is loaded entirely client-side, so it needed a live browser
    session reading real network requests to find, the same way this
    project found Workday tenants via WebSearch instead — except ORC
    doesn't turn up in web search the way Workday tenants do, since its
    host is an opaque per-company hash (Honeywell's is "ibqbjb"), not a
    guessable subdomain.

    entry needs: {ats: oracle_recruiting, host, site_number, job_detail_base, category, max_pages?}
    `host` and `site_number` come straight off a real request in
    DevTools -> Network while browsing the company's own careers site,
    e.g. for Honeywell:
      https://ibqbjb.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/
      recruitingCEJobRequisitions?...finder=findReqs;siteNumber=CX_1,...
    gives host="ibqbjb.fa.ocs.oraclecloud.com", site_number="CX_1".
    `job_detail_base` is the public career-site URL prefix a requisition
    ID gets appended to, e.g. "https://careers.honeywell.com/en/sites/
    Honeywell/job" (confirmed live: {job_detail_base}/{Id} resolves,
    found by clicking an actual job link in the site's own UI rather
    than guessed).
    """

    name = "oracle_recruiting"

    def fetch(self, entry: dict) -> list[Posting]:
        host = entry.get("host")
        site_number = entry.get("site_number")
        job_detail_base = entry.get("job_detail_base")
        missing = [k for k, v in [("host", host), ("site_number", site_number), ("job_detail_base", job_detail_base)] if not v]
        if missing:
            raise ValueError(f"oracle_recruiting entry for {entry.get('company')} is missing {missing}")

        url = API.format(host=host)
        limit = 25
        offset = 0
        total = None
        max_pages = entry.get("max_pages", 60)
        page = 0
        postings = []

        while True:
            finder = f"findReqs;siteNumber={site_number},limit={limit},offset={offset},sortBy=POSTING_DATES_DESC"
            resp = requests.get(
                url,
                params={"onlyData": "true", "expand": "requisitionList", "finder": finder},
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0 (internship-feed-bot)"},
            )
            if resp.status_code != 200:
                raise ValueError(
                    f"oracle_recruiting host='{host}' site_number='{site_number}' for {entry.get('company')} "
                    f"returned HTTP {resp.status_code} — check host/site_number against the live careers site "
                    "(docs/adding-a-source.md)"
                )
            data = resp.json()
            items = data.get("items")
            if not items:
                raise ValueError(f"unexpected oracle_recruiting response shape for {entry.get('company')}: {list(data.keys())}")
            block = items[0]
            if total is None:
                total = block.get("TotalJobsCount", 0)

            reqs = block.get("requisitionList") or []
            for r in reqs:
                job_id = r.get("Id")
                description = " ".join(
                    filter(None, [r.get("ShortDescriptionStr"), r.get("ExternalQualificationsStr"), r.get("ExternalResponsibilitiesStr")])
                )
                postings.append(
                    Posting(
                        id=f"oracle_recruiting:{host}:{job_id}",
                        company=entry.get("company", ""),
                        title=r.get("Title", ""),
                        location=r.get("PrimaryLocation", ""),
                        url=f"{job_detail_base}/{job_id}",
                        source="oracle_recruiting",
                        category=entry.get("category", ""),
                        posted_at=r.get("PostedDate"),
                        description_snippet=strip_html(description),
                        description=to_display_text(description),
                    )
                )

            page += 1
            offset += limit
            if not reqs or offset >= total or page >= max_pages:
                break

        return postings
