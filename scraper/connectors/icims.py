import re

import requests

from .base import Connector, Posting
from .util import strip_html, to_display_text

# iCIMS' candidate-facing board has no public JSON API (confirmed live) --
# every listing is server-rendered HTML, fetched through the SAME
# "in_iframe" request the real page's own iframe makes rather than the
# outer /jobs/search page (confirmed live: the outer page's HTML is
# nearly empty; all of the actual job list markup only appears in this
# iframe response).
LISTING_URL = (
    "https://careers-{slug}.icims.com/jobs/search"
    "?pr={page}&in_iframe=1&mobile=false&width=1200&height=500"
    "&bga=true&needsRedirect=false&jan1offset=-360&jun1offset=-300"
)

# One job card's markup (confirmed live against a real board):
#   <li class="iCIMS_JobCardItem"> ... </li>
# split on the opening tag rather than matched as a balanced block --
# simpler and safe here since cards never nest.
CARD_SPLIT_RE = re.compile(r'<li class="iCIMS_JobCardItem">')
LOCATION_RE = re.compile(r"Job Locations</span>\s*<span[^>]*>\s*([^<]+?)\s*</span>", re.DOTALL)
JOB_ID_RE = re.compile(r"Job ID</span>\s*<span[^>]*>\s*([^<]+?)\s*</span>", re.DOTALL)
LINK_RE = re.compile(r'<a href="([^"]+)" class="iCIMS_Anchor"')
TITLE_RE = re.compile(r"<h3\s*>\s*([^<]+?)\s*</h3>")
DESCRIPTION_RE = re.compile(r'<div class="col-xs-12 description">(.*?)</div>', re.DOTALL)
PAGE_COUNT_RE = re.compile(r"Page\s+\d+\s+of\s+(\d+)")

# iCIMS locations read like "US-GA-Atlanta" (country-state-city). Anything
# not matching that exact 3-part US shape is passed through as-is rather
# than guessed at -- confirmed live this is the format for a US-based
# board; non-US boards weren't seen during development.
_US_LOCATION_RE = re.compile(r"^([A-Z]{2})-([A-Z]{2})-(.+)$")


def _humanize_location(raw: str) -> str:
    m = _US_LOCATION_RE.match(raw.strip())
    if m and m.group(1) == "US":
        return f"{m.group(3)}, {m.group(2)}"
    return raw.strip()


class IcimsConnector(Connector):
    """iCIMS' public candidate-facing job board. No auth required, no
    JSON API -- HTML scraped from the same iframe URL the real page's own
    embedded results panel loads.

    entry needs: {ats: icims, company: "Display Name", slug: "federatedinsurance", category: "..."}
    `slug` is the subdomain label in the company's own board URL:
    careers-<slug>.icims.com

    Paginated via the `pr` query param, which is a 0-indexed PAGE number
    (confirmed live: page 2's link is `pr=1`, not a row offset).
    """

    name = "icims"

    def fetch(self, entry: dict) -> list[Posting]:
        slug = entry.get("slug")
        if not slug:
            raise ValueError(f"icims entry for {entry.get('company')} is missing 'slug'")

        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
        max_pages = entry.get("max_pages", 20)
        postings = []
        total_pages = None
        page = 0

        while page < max_pages and (total_pages is None or page < total_pages):
            resp = requests.get(LISTING_URL.format(slug=slug, page=page), headers=headers, timeout=20)
            if resp.status_code != 200:
                if page == 0:
                    raise ValueError(
                        f"icims slug '{slug}' for {entry.get('company')} returned HTTP {resp.status_code} — "
                        "the slug is wrong or the board doesn't exist"
                    )
                break

            if total_pages is None:
                count_match = PAGE_COUNT_RE.search(resp.text)
                total_pages = int(count_match.group(1)) if count_match else 1

            cards = CARD_SPLIT_RE.split(resp.text)[1:]
            if not cards:
                break

            for card in cards:
                link_match = LINK_RE.search(card)
                title_match = TITLE_RE.search(card)
                if not link_match or not title_match:
                    continue
                url = link_match.group(1)
                title = title_match.group(1).strip()

                job_id_match = JOB_ID_RE.search(card)
                job_id = job_id_match.group(1).strip() if job_id_match else url

                location_match = LOCATION_RE.search(card)
                location = _humanize_location(location_match.group(1)) if location_match else ""

                desc_match = DESCRIPTION_RE.search(card)
                description_raw = desc_match.group(1) if desc_match else ""

                postings.append(
                    Posting(
                        id=f"icims:{slug}:{job_id}",
                        company=entry.get("company", slug),
                        title=title,
                        location=location,
                        url=url,
                        source="icims",
                        category=entry.get("category", ""),
                        description_snippet=strip_html(description_raw),
                        description=to_display_text(description_raw),
                    )
                )
            page += 1

        return postings
