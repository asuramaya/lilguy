import requests

from .base import Connector, Posting
from .util import strip_html, to_display_text

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


class GreenhouseConnector(Connector):
    """Greenhouse's public job-board API. No auth required.

    entry needs: {ats: greenhouse, company: "Display Name", token: "board-token", category: "..."}
    The token is the slug in the company's own Greenhouse board URL:
    boards.greenhouse.io/<token>
    """

    name = "greenhouse"

    def fetch(self, entry: dict) -> list[Posting]:
        token = entry.get("token")
        if not token:
            raise ValueError(f"greenhouse entry for {entry.get('company')} is missing 'token'")
        resp = requests.get(API.format(token=token), timeout=20)
        if resp.status_code == 404:
            raise ValueError(
                f"greenhouse token '{token}' for {entry.get('company')} returned 404 — "
                "the token is wrong or the board doesn't exist"
            )
        resp.raise_for_status()
        data = resp.json()
        if "jobs" not in data:
            raise ValueError(f"unexpected greenhouse response shape for token '{token}': {list(data.keys())}")

        postings = []
        for job in data["jobs"]:
            location = (job.get("location") or {}).get("name", "")
            postings.append(
                Posting(
                    id=f"greenhouse:{token}:{job['id']}",
                    company=entry.get("company", token),
                    title=job.get("title", ""),
                    location=location,
                    url=job.get("absolute_url", ""),
                    source="greenhouse",
                    category=entry.get("category", ""),
                    posted_at=job.get("updated_at"),
                    description_snippet=strip_html(job.get("content", "")),
                    description=to_display_text(job.get("content", "")),
                )
            )
        return postings
