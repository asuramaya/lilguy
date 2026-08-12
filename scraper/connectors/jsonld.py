import json
import re

import requests

from .base import Connector, Posting
from .util import strip_html

LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.DOTALL
)
LOC_RE = re.compile(r"<loc>([^<]+)</loc>")


class JsonLdConnector(Connector):
    """Generic schema.org/JobPosting harvester — the technique most large
    corporate career sites already implement for Google-for-Jobs SEO,
    independent of which ATS vendor is underneath. This is what makes it
    genuinely general rather than one more per-vendor connector: the same
    code found real postings on UPS's Phenom-People-hosted career site,
    an ATS none of the other connectors in this project support.

    Two-step crawl: read an XML sitemap for job-detail URLs matching
    `url_pattern`, then fetch each (up to `max_pages`) and pull out the
    JobPosting block from its JSON-LD. A page can embed several JSON-LD
    blocks for unrelated things (breadcrumbs, generic WebPage schema) —
    confirmed live on UPS's own job pages, which ship all three — so this
    only keeps the block whose @type is literally "JobPosting" and skips
    the rest rather than guessing which one matters.

    entry needs: {ats: jsonld, company, sitemap_url, url_pattern, category, max_pages?}
    `url_pattern` is a plain substring (not a full regex) checked against
    each sitemap URL, e.g. "/job/" — found per-company the same way
    docs/adding-a-source.md describes for an ATS token: open the site's
    sitemap.xml/sitemap_index.xml (usually linked from robots.txt), find
    which one actually lists individual job pages (several sites publish
    multiple sitemaps — page/category sitemaps as well as a job sitemap —
    and only the latter is useful here), and see what the job URLs share.
    """

    name = "jsonld"

    def fetch(self, entry: dict) -> list[Posting]:
        sitemap_url = entry.get("sitemap_url")
        url_pattern = entry.get("url_pattern")
        if not sitemap_url or not url_pattern:
            raise ValueError(f"jsonld entry for {entry.get('company')} needs both 'sitemap_url' and 'url_pattern'")

        resp = requests.get(sitemap_url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (internship-feed-bot)"})
        resp.raise_for_status()
        job_urls = [u for u in LOC_RE.findall(resp.text) if url_pattern in u]
        if not job_urls:
            raise ValueError(
                f"jsonld sitemap for {entry.get('company')} ({sitemap_url}) returned 0 URLs matching "
                f"'{url_pattern}' — the sitemap moved, or url_pattern needs updating"
            )

        max_pages = entry.get("max_pages", 60)
        postings = []
        for url in job_urls[:max_pages]:
            posting = self._fetch_one(url, entry)
            if posting:
                postings.append(posting)
        return postings

    def _fetch_one(self, url: str, entry: dict):
        try:
            resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (internship-feed-bot)"})
            resp.raise_for_status()
        except requests.RequestException:
            return None  # one dead link in a 400-url sitemap shouldn't fail the whole source

        for block in LD_JSON_RE.findall(resp.text):
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            if data.get("@type") != "JobPosting":
                continue

            org = (data.get("hiringOrganization") or {}).get("name", entry.get("company", ""))
            loc = data.get("jobLocation") or {}
            address = loc.get("address", {}) if isinstance(loc, dict) else {}
            location = ", ".join(
                filter(None, [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")])
            )
            job_id = (data.get("identifier") or {}).get("value", url)

            return Posting(
                id=f"jsonld:{entry.get('company')}:{job_id}",
                company=org,
                title=data.get("title", ""),
                location=location,
                url=url,
                source="jsonld",
                category=entry.get("category", ""),
                posted_at=data.get("datePosted"),
                description_snippet=strip_html(data.get("description", "")),
            )
        return None
