import sys
from pathlib import Path
from typing import Any

import requests

from .base import Connector, Posting
from .util import to_display_text

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))
from work_arrangement import HYBRID, REMOTE, normalise  # noqa: E402

API = "https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs"


class RipplingConnector(Connector):
    """Rippling public ATS job-board connector. No auth, public JSON endpoint.

    entry: {ats: rippling, company: "Display Name", token: "company-slug", category: "..."}
    Board URL: https://ats.rippling.com/<token>/jobs
    """

    name = "rippling"

    def fetch(self, entry: dict) -> list[Posting]:
        token = entry.get("token")
        if not token:
            raise ValueError(f"rippling entry for {entry.get('company')} is missing 'token'")

        url = API.format(token=token)
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
            timeout=20,
        )
        if resp.status_code == 404:
            raise ValueError(
                f"rippling token '{token}' for {entry.get('company')} returned 404 — "
                "the token is wrong or the board does not exist"
            )
        resp.raise_for_status()
        data = resp.json()

        jobs = data if isinstance(data, list) else data.get("jobs", [])
        postings = []

        for job in jobs:
            job_id = str(job.get("id") or job.get("uuid") or "")
            if not job_id:
                continue

            title = (job.get("name") or job.get("title") or "").strip()
            if not title:
                continue

            # Location and work arrangement
            loc_data = job.get("location") or {}
            if isinstance(loc_data, dict):
                city = loc_data.get("city") or ""
                state = loc_data.get("state") or loc_data.get("region") or ""
                country = loc_data.get("country") or ""
                loc_parts = [p for p in (city, state, country) if p]
                loc_str = ", ".join(loc_parts) if loc_parts else "Multiple Locations"
            else:
                loc_str = str(loc_data or "").strip() or "Multiple Locations"

            work_type = str(job.get("workplaceType") or job.get("locationType") or "").lower()
            if "remote" in work_type:
                arrangement = REMOTE
            elif "hybrid" in work_type:
                arrangement = HYBRID
            else:
                arrangement = normalise(loc_str)

            job_url = job.get("url") or f"https://ats.rippling.com/{token}/jobs/{job_id}"
            description_raw = job.get("description") or job.get("descriptionHtml") or ""
            description_text = to_display_text(description_raw)
            snippet = " ".join(description_text.split()[:80])

            postings.append(
                Posting(
                    id=f"rippling:{token}:{job_id}",
                    company=entry.get("company", token),
                    title=title,
                    location=loc_str,
                    url=job_url,
                    source=self.name,
                    category=entry.get("category", ""),
                    work_arrangement=arrangement,
                    posted_at=job.get("created_at") or job.get("updated_at"),
                    description_snippet=snippet,
                    description=description_text,
                )
            )

        return postings
