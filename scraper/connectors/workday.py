import requests

from .base import Connector, Posting
from .util import strip_html

URL = "https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"


class WorkdayConnector(Connector):
    """Workday's job-search JSON API (POST, no auth required for public postings).

    entry needs: {ats: workday, company: "Display Name", tenant, wd_host, site, category}
    wd_host is Workday's per-tenant pod, e.g. "wd1" or "wd5" — it's the
    subdomain segment right after the tenant name in the company's careers
    URL: https://<tenant>.<wd_host>.myworkdayjobs.com/...
    See docs/adding-a-source.md for how to read these off a live careers
    page with browser devtools — they are NOT guessable from the company
    name, and a wrong tenant/site/host fails loudly here rather than
    silently returning zero postings.
    """

    name = "workday"

    def fetch(self, entry: dict) -> list[Posting]:
        missing = [k for k in ("tenant", "wd_host", "site") if not entry.get(k)]
        if missing:
            raise ValueError(f"workday entry for {entry.get('company')} is missing {missing}")

        url = URL.format(tenant=entry["tenant"], wd_host=entry["wd_host"], site=entry["site"])
        postings: list[Posting] = []
        offset = 0
        limit = 20
        while True:
            resp = requests.post(
                url,
                json={"limit": limit, "offset": offset, "searchText": ""},
                headers={"Content-Type": "application/json"},
                timeout=20,
            )
            if resp.status_code != 200:
                raise ValueError(
                    f"workday tenant='{entry['tenant']}' site='{entry['site']}' host='{entry['wd_host']}' "
                    f"for {entry.get('company')} returned HTTP {resp.status_code} — "
                    "check tenant/site/wd_host against the live careers page (docs/adding-a-source.md)"
                )
            data = resp.json()
            if "jobPostings" not in data:
                raise ValueError(f"unexpected workday response shape for {entry.get('company')}: {list(data.keys())}")

            batch = data["jobPostings"]
            for job in batch:
                external_path = job.get("externalPath", "")
                postings.append(
                    Posting(
                        id=f"workday:{entry['tenant']}:{entry['site']}:{external_path}",
                        company=entry.get("company", entry["tenant"]),
                        title=job.get("title", ""),
                        location=job.get("locationsText", ""),
                        url=f"https://{entry['tenant']}.{entry['wd_host']}.myworkdayjobs.com/{entry['site']}{external_path}",
                        source="workday",
                        category=entry.get("category", ""),
                        posted_at=job.get("postedOn"),
                        description_snippet=strip_html(job.get("bulletFields", [""])[0] if job.get("bulletFields") else ""),
                    )
                )

            total = data.get("total", len(batch))
            offset += limit
            if offset >= total or not batch:
                break

        return postings
