import sys
import time

import requests

from .base import Connector, Posting
from .util import strip_html, to_display_text

API = "https://www.themuse.com/api/public/jobs"


class MuseConnector(Connector):
    """The Muse's public jobs API — a real cross-company aggregator, not a
    per-company board. No API key required for reasonable use (their docs
    cite a 500 req/hour cap without one). This is the "general/abstract"
    source: one entry here covers however many companies The Muse has
    indexed under the given level (and category, if set), with zero
    per-company setup.

    entry needs: {ats: muse, level: "Internship", category? | categories?, max_pages?}
    `category` (singular) or `categories` (a list) must be one of The
    Muse's own taxonomy strings (case-sensitive, e.g. "Business
    Operations"). Leave both unset to pull EVERY internship-level posting
    in one blended query.

    IMPORTANT, confirmed live: a single blended (no-category) query and
    per-category queries are NOT equivalent once any one category's real
    total exceeds the API's own pagination depth limit (~1900 results,
    see max_pages below) — "Healthcare" alone has ~3500 internship-level
    postings, so a blended query's first ~1900 results skew overwhelmingly
    Healthcare, and everything else (Business Operations, Transportation
    and Logistics, Sales...) gets starved to whatever scraps are left,
    even though each of THOSE categories' own true total is well under
    the depth limit and would be fully captured by its own query.
    `categories` (plural, a list) queries each one separately and unions
    the results (deduped by job id — a job can legitimately appear under
    more than one Muse category), which is why sources.yaml uses it
    rather than one unbounded query.

    RESILIENCE, confirmed necessary live: querying ~250+ pages across 33
    categories means SOME single request eventually hits a transient
    network hiccup — this happened for real (one HTTP read timeout out of
    ~250 requests) and, before this was hardened, took down the entire
    connector's contribution for that run (0 postings returned instead of
    ~4200), which store.py's rebuild() then read as "every one of those
    postings closed" and nearly wiped the raw store. Two layers now:
    (1) `_get_with_retry` retries a single failed request a couple of
    times with backoff before giving up on it; (2) in the `categories`
    loop, one category that still fails after retries is skipped (logged
    to stderr) rather than aborting every other category — this connector
    only raises if EVERY category failed, since a genuinely broken config
    should still fail loudly rather than silently return nothing.

    `level` defaults to "Internship". `max_pages` caps how many 20-result
    pages to walk PER CATEGORY (or for the single/no-category case).
    `company` in sources.yaml is a display label here, not a lookup key —
    an aggregator entry doesn't target one company.
    """

    name = "muse"

    def fetch(self, entry: dict) -> list[Posting]:
        level = entry.get("level", "Internship")
        max_pages = entry.get("max_pages")
        categories = entry.get("categories")

        if categories:
            by_id: dict[str, Posting] = {}
            failures = []
            for category in categories:
                try:
                    for posting in self._fetch_one(category, level, max_pages):
                        by_id[posting.id] = posting  # dedup: a job can appear under >1 category
                except Exception as exc:  # noqa: BLE001 - one bad category must not kill the rest
                    failures.append(f"{category}: {exc}")
                    print(f"    [muse] category '{category}' failed, skipping: {exc}", file=sys.stderr)
            if failures and len(failures) == len(categories):
                raise ValueError(f"muse: every category failed: {'; '.join(failures)}")
            return list(by_id.values())

        return self._fetch_one(entry.get("category"), level, max_pages)

    def _get_with_retry(self, params: dict, attempts: int = 3):
        last_exc = None
        for attempt in range(attempts):
            try:
                return requests.get(API, params=params, timeout=20)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    time.sleep(1.5 * (attempt + 1))
        raise last_exc

    def _fetch_one(self, category, level: str, max_pages) -> list[Posting]:
        postings = []
        page = 1
        page_count = 1
        while page <= page_count:
            if max_pages and page > max_pages:
                break
            params = {"level": level, "page": page}
            if category:
                params["category"] = category
            resp = self._get_with_retry(params)
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
                        # The Muse indexes by JOB FUNCTION, so that is
                        # what this value is -- it was previously written
                        # to `category`, which everywhere else in this
                        # project means the employer's industry. The
                        # industry of a Muse employer is genuinely
                        # unknown to us, so it stays empty.
                        job_function=category or ", ".join(job_categories) or "",
                        posted_at=job.get("publication_date"),
                        description_snippet=strip_html(job.get("contents", "")),
                        description=to_display_text(job.get("contents", "")),
                    )
                )
            page += 1

        return postings
