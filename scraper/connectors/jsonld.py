import json
import re
import time

import requests

from .base import Connector, Posting
from .util import strip_html, to_display_text

LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.DOTALL
)
LOC_RE = re.compile(r"<loc>([^<]+)</loc>")


def _addr_field(value):
    # schema.org allows addressCountry etc. to be either a plain string
    # ("US") or a nested Country/AdministrativeArea object with its own
    # "name" — confirmed live on Eaton's Eightfold-hosted JobPosting JSON-
    # LD, which uses the nested-object form and crashed a plain ", ".join
    # over the raw values.
    if isinstance(value, dict):
        return value.get("name", "")
    return value

# Confirmed live against RTX's careers site: firing requests back-to-back
# with zero pacing gets intermittently 403'd (a WAF/bot-detection burst
# trigger, not a User-Agent block — plain curl with the SAME headers hit
# the same URLs fine when run interactively, i.e. with natural pauses
# between calls; the Python connector making rapid sequential requests
# didn't). Adding a small delay between every outbound request eliminated
# it entirely. Applied to every connector request (sitemap fetches AND
# individual job-page fetches, since a single source can mean dozens to
# hundreds of the latter) — this is a general reliability fix, not a
# workaround specific to RTX, since any career site could have similar
# bot-detection this project hasn't triggered yet just because it hasn't
# been tested against it.
REQUEST_DELAY_SECONDS = 0.4


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

    `sitemap_url` can point at a real sitemap OR a sitemap-index (a
    sitemap of sitemaps) — confirmed live that some companies split job
    listings across several numbered sitemaps under one index (RTX: 9
    sub-sitemaps). Expanded automatically, one level of index nesting
    (a sub-sitemap that's itself another index isn't followed further —
    hasn't been needed yet; raise the depth cap in _collect_locs if it
    ever is), capped at 25 sub-sitemaps so a runaway index can't turn one
    source into an unbounded crawl.
    """

    name = "jsonld"

    def fetch(self, entry: dict) -> list[Posting]:
        sitemap_url = entry.get("sitemap_url")
        url_pattern = entry.get("url_pattern")
        if not sitemap_url or not url_pattern:
            raise ValueError(f"jsonld entry for {entry.get('company')} needs both 'sitemap_url' and 'url_pattern'")

        all_locs = self._collect_locs(sitemap_url)
        job_urls = [u for u in all_locs if url_pattern in u]
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

    def _get(self, url: str, timeout: int):
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (internship-feed-bot)"})
        time.sleep(REQUEST_DELAY_SECONDS)
        return resp

    def _get_sitemap_with_retry(self, url: str, timeout: int, attempts: int = 3):
        # A sitemap fetch failing kills the ENTIRE source (unlike a single
        # job page, which _fetch_one already tolerates) — confirmed live
        # on RTX, where one transient 403 on a sub-sitemap during a run
        # dropped the whole source despite the request-pacing fix already
        # in place. Same shape as the Muse per-category retry: one bad
        # request shouldn't be fatal when a short retry would likely
        # succeed.
        last_exc = None
        for attempt in range(attempts):
            try:
                resp = self._get(url, timeout=timeout)
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    time.sleep(1.5 * (attempt + 1))
        raise last_exc

    def _collect_locs(self, sitemap_url: str, depth: int = 0, max_sub_sitemaps: int = 25) -> list[str]:
        resp = self._get_sitemap_with_retry(sitemap_url, timeout=20)
        locs = LOC_RE.findall(resp.text)

        # A sitemap-index's <loc> entries are themselves sitemap files
        # (all end in .xml); a real per-page sitemap's entries are actual
        # pages, which essentially never do. If every entry looks like a
        # sitemap, treat it as an index and expand one level. Check the
        # URL's path, not the raw string — confirmed live on Eaton's
        # Eightfold-hosted sitemap, whose sub-sitemap URLs carry a
        # trailing query string (".../sitemap.xml?domain=eaton.com"),
        # which made a naive `.endswith(".xml")` always false and left
        # the index's own 2 URLs being read as if they were job pages.
        looks_like_index = bool(locs) and all(
            loc.lower().split("?", 1)[0].endswith(".xml") for loc in locs
        )
        if looks_like_index and depth < 1:
            expanded = []
            for sub in locs[:max_sub_sitemaps]:
                expanded.extend(self._collect_locs(sub, depth + 1, max_sub_sitemaps))
            return expanded
        return locs

    def _fetch_one(self, url: str, entry: dict):
        try:
            resp = self._get(url, timeout=15)
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
                filter(None, [
                    _addr_field(address.get("addressLocality")),
                    _addr_field(address.get("addressRegion")),
                    _addr_field(address.get("addressCountry")),
                ])
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
                description=to_display_text(data.get("description", "")),
            )
        return None
