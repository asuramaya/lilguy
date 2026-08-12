import os

import requests

from .base import Connector, Posting

API = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"


class AdzunaConnector(Connector):
    """Adzuna — a real cross-board job aggregator (indexes many companies'
    postings, not one company's own ATS). Free tier, but requires YOUR OWN
    app_id/app_key (2-minute signup: https://developer.adzuna.com/) — set
    them as the ADZUNA_APP_ID / ADZUNA_APP_KEY environment variables, never
    in sources.yaml or committed to git. Unlike the Muse connector, this
    one is documented but not verified end-to-end in this repo, since doing
    so needs credentials nobody here holds — it fails loudly with a clear
    message if the env vars aren't set, rather than silently doing nothing.

    entry needs: {ats: adzuna, what: "supply chain intern OR logistics intern", category_label: "...", country: "us"}
    `what` is Adzuna's free-text search query — combine an intern-track term
    with a domain term the same way filters.py does, since Adzuna's own
    category taxonomy is industry-level (e.g. "logistics-warehouse-jobs"),
    not intern-vs-not.
    """

    name = "adzuna"

    def fetch(self, entry: dict) -> list[Posting]:
        app_id = os.environ.get("ADZUNA_APP_ID")
        app_key = os.environ.get("ADZUNA_APP_KEY")
        if not app_id or not app_key:
            raise ValueError(
                "ADZUNA_APP_ID / ADZUNA_APP_KEY not set — get a free key at "
                "https://developer.adzuna.com/ and export both as env vars "
                "(GitHub Actions: add them as repo secrets)"
            )
        what = entry.get("what")
        if not what:
            raise ValueError("adzuna entry is missing 'what' (a search query, e.g. 'supply chain intern')")
        country = entry.get("country", "us")

        postings = []
        page = 1
        max_pages = entry.get("max_pages", 5)  # 50/page cap * 5 = 250 results ceiling per query, adjust as needed
        while page <= max_pages:
            resp = requests.get(
                API.format(country=country, page=page),
                params={
                    "app_id": app_id,
                    "app_key": app_key,
                    "what": what,
                    "results_per_page": 50,
                    "content-type": "application/json",
                },
                timeout=20,
            )
            if resp.status_code != 200:
                raise ValueError(f"adzuna query '{what}' returned HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            results = data.get("results", [])
            if not results:
                break

            for job in results:
                postings.append(
                    Posting(
                        id=f"adzuna:{job['id']}",
                        company=(job.get("company") or {}).get("display_name", ""),
                        title=job.get("title", ""),
                        location=(job.get("location") or {}).get("display_name", ""),
                        url=job.get("redirect_url", ""),
                        source="adzuna",
                        category=entry.get("category_label", ""),
                        posted_at=job.get("created"),
                        description_snippet=(job.get("description") or "")[:600],
                    )
                )
            page += 1

        return postings
