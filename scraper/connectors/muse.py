import requests

from .base import Connector, Posting
from .util import strip_html

API = "https://www.themuse.com/api/public/jobs"


class MuseConnector(Connector):
    """The Muse's public jobs API — a real cross-company aggregator, not a
    per-company board. No API key required for reasonable use (their docs
    cite a 500 req/hour cap without one). This is the "general/abstract"
    source: one entry here covers however many companies The Muse has
    indexed under the given level (and category, if set), with zero
    per-company setup.

    entry needs: {ats: muse, level: "Internship", category?, category_label?, max_pages?}
    `category`, if set, must be one of The Muse's own taxonomy strings
    (case-sensitive, e.g. "Business Operations"). Leave it unset to pull
    EVERY internship-level posting regardless of category — this is the
    "as much as possible" mode: broader raw coverage for downstream
    filters.yaml/user_filter.py to narrow however each reader wants,
    rather than this connector pre-deciding what's relevant. `level`
    defaults to "Internship". `max_pages` caps how many 20-result pages to
    walk (unset category means ~400 pages / ~8000 postings as of when this
    was written — set max_pages if a full daily sweep is more than you
    want). `company` in sources.yaml is a display label here, not a lookup
    key — an aggregator entry doesn't target one company.
    """

    name = "muse"

    def fetch(self, entry: dict) -> list[Posting]:
        category = entry.get("category")
        level = entry.get("level", "Internship")
        max_pages = entry.get("max_pages")

        postings = []
        page = 1
        page_count = 1
        while page <= page_count:
            if max_pages and page > max_pages:
                break
            params = {"level": level, "page": page}
            if category:
                params["category"] = category
            resp = requests.get(API, params=params, timeout=20)
            label = category or "all categories"
            if resp.status_code != 200:
                raise ValueError(f"muse category='{label}' returned HTTP {resp.status_code}")
            data = resp.json()
            if "results" not in data:
                raise ValueError(f"unexpected muse response shape for category='{label}': {list(data.keys())}")

            page_count = data.get("page_count", 1)
            for job in data["results"]:
                company = (job.get("company") or {}).get("name", "")
                locations = ", ".join(loc.get("name", "") for loc in job.get("locations", []))
                job_categories = [c.get("name", "") for c in job.get("categories", [])]
                postings.append(
                    Posting(
                        id=f"muse:{job['id']}",
                        company=company,
                        title=job.get("name", ""),
                        location=locations,
                        url=job.get("refs", {}).get("landing_page", ""),
                        source="muse",
                        category=entry.get("category_label") or category or ", ".join(job_categories) or "Uncategorized",
                        posted_at=job.get("publication_date"),
                        description_snippet=strip_html(job.get("contents", "")),
                    )
                )
            page += 1

        return postings
