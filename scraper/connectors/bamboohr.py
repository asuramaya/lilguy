import requests

from .base import Connector, Posting
from .util import strip_html, to_display_text

API = "https://{token}.bamboohr.com/careers/list"


class BambooHRConnector(Connector):
    """BambooHR's public candidate-facing job-board API. No auth required.

    entry needs: {ats: bamboohr, company: "Display Name", token: "account-slug", category: "..."}
    The token is the subdomain in the company's own board URL:
    <token>.bamboohr.com/careers

    Confirmed live: GET {token}.bamboohr.com/careers/list returns
    {"meta": {"totalCount": N}, "result": [...]}, unauthenticated, and a
    wrong/nonexistent token redirects to bamboohr.com's own marketing
    site rather than 404ing -- distinguished below by requiring the
    `result` key to actually be present, not just a 200 status.
    Individual job field names below follow BambooHR's documented public
    careers-list schema; not confirmed against a live account with open
    postings this session (every real token tried had zero current
    openings) -- verify field names against a real response once this
    catches a live posting in production.
    """

    name = "bamboohr"

    def fetch(self, entry: dict) -> list[Posting]:
        token = entry.get("token")
        if not token:
            raise ValueError(f"bamboohr entry for {entry.get('company')} is missing 'token'")

        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
        resp = requests.get(API.format(token=token), headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if "result" not in data:
            raise ValueError(
                f"bamboohr token '{token}' for {entry.get('company')} doesn't look like a real board — "
                "the token is wrong or the account doesn't exist"
            )

        postings = []
        for job in data["result"]:
            job_id = str(job.get("id") or "")
            title = (job.get("jobOpeningName") or job.get("title") or "").strip()
            if not job_id or not title:
                continue

            location_obj = job.get("location") or {}
            if isinstance(location_obj, dict):
                loc_parts = [location_obj.get("city"), location_obj.get("state"), location_obj.get("country")]
                location = ", ".join(p for p in loc_parts if p) or "Not specified"
            else:
                location = str(location_obj or "Not specified")
            if job.get("isRemote"):
                location = "Remote"

            description_raw = job.get("description") or ""
            job_url = f"https://{token}.bamboohr.com/careers/{job_id}"

            postings.append(
                Posting(
                    id=f"bamboohr:{token}:{job_id}",
                    company=entry.get("company", token),
                    title=title,
                    location=location,
                    url=job_url,
                    source="bamboohr",
                    category=entry.get("category", ""),
                    posted_at=job.get("postedDate") or job.get("datePosted"),
                    description_snippet=strip_html(description_raw),
                    description=to_display_text(description_raw),
                )
            )
        return postings
