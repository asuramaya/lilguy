import requests

from .base import Connector, Posting

API = "https://api.lever.co/v0/postings/{company}?mode=json"


class LeverConnector(Connector):
    """Lever's public postings API. No auth required.

    entry needs: {ats: lever, company: "Display Name", token: "lever-company-slug", category: "..."}
    The token is the slug in the company's own Lever board URL:
    jobs.lever.co/<token>
    """

    name = "lever"

    def fetch(self, entry: dict) -> list[Posting]:
        token = entry.get("token")
        if not token:
            raise ValueError(f"lever entry for {entry.get('company')} is missing 'token'")
        resp = requests.get(API.format(company=token), timeout=20)
        if resp.status_code == 404:
            raise ValueError(
                f"lever token '{token}' for {entry.get('company')} returned 404 — "
                "the token is wrong or the board doesn't exist"
            )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError(f"unexpected lever response shape for token '{token}': {type(data)}")

        postings = []
        for job in data:
            categories = job.get("categories", {}) or {}
            location = categories.get("location", "")
            postings.append(
                Posting(
                    id=f"lever:{token}:{job['id']}",
                    company=entry.get("company", token),
                    title=job.get("text", ""),
                    location=location,
                    url=job.get("hostedUrl", ""),
                    source="lever",
                    category=entry.get("category", ""),
                    posted_at=job.get("createdAt"),
                    description_snippet=(job.get("descriptionPlain") or "")[:600],
                )
            )
        return postings
