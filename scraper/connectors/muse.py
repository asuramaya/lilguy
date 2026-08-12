import requests

from .base import Connector, Posting
from .util import strip_html

API = "https://www.themuse.com/api/public/jobs"


class MuseConnector(Connector):
    """The Muse's public jobs API — a real cross-company aggregator, not a
    per-company board. No API key required for reasonable use (their docs
    cite a 500 req/hour cap without one). This is the "general/abstract"
    source: one entry here covers however many companies The Muse has
    indexed under the given level/category, with zero per-company setup.

    entry needs: {ats: muse, category: "Business Operations", category_label: "...", level: "Internship"}
    `category` must be one of The Muse's own taxonomy strings (case-sensitive,
    e.g. "Business Operations" — confirmed live to surface supply-chain,
    procurement, and logistics-adjacent internships despite there being no
    "Supply Chain" category itself). `level` defaults to "Internship".
    `company` in sources.yaml is used as a display label here, not a lookup
    key — an aggregator entry doesn't target one company.
    """

    name = "muse"

    def fetch(self, entry: dict) -> list[Posting]:
        category = entry.get("category")
        if not category:
            raise ValueError("muse entry is missing 'category' (a Muse taxonomy string, e.g. 'Business Operations')")
        level = entry.get("level", "Internship")

        postings = []
        page = 1
        page_count = 1
        while page <= page_count:
            resp = requests.get(
                API,
                params={"level": level, "category": category, "page": page},
                timeout=20,
            )
            if resp.status_code != 200:
                raise ValueError(f"muse category='{category}' returned HTTP {resp.status_code}")
            data = resp.json()
            if "results" not in data:
                raise ValueError(f"unexpected muse response shape for category='{category}': {list(data.keys())}")

            page_count = data.get("page_count", 1)
            for job in data["results"]:
                company = (job.get("company") or {}).get("name", "")
                locations = ", ".join(loc.get("name", "") for loc in job.get("locations", []))
                postings.append(
                    Posting(
                        id=f"muse:{job['id']}",
                        company=company,
                        title=job.get("name", ""),
                        location=locations,
                        url=job.get("refs", {}).get("landing_page", ""),
                        source="muse",
                        category=entry.get("category_label", category),
                        posted_at=job.get("publication_date"),
                        description_snippet=strip_html(job.get("contents", "")),
                    )
                )
            page += 1

        return postings
