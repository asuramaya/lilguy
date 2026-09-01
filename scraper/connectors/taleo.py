import re
import xml.etree.ElementTree as ET

import requests

from .base import Connector, Posting
from .util import strip_html, to_display_text

JOBSEARCH_URL = "https://{tenant}.taleo.net/careersection/{section}/jobsearch.ftl?lang=en"
RSS_URL = (
    "https://{tenant}.taleo.net/careersection/feed/joblist.rss"
    "?lang=en&portal={portal}&searchtype=3&f=null&s=2|A&multiline=true"
)
JOBDETAIL_URL = "https://{tenant}.taleo.net/careersection/{section}/jobdetail.ftl?job={job_id}"

# Confirmed live (WIPO's board): the portal id used by the RSS feed isn't
# in the page's visible HTML, only in an inline JS variable --
# `queryString: 'lang=en&amp;portal=10105120713'` -- portal isn't
# necessarily the first param in that string (lang= led it here), and
# the ampersand is HTML-entity-encoded, so this matches "portal=" any-
# where inside the quoted value rather than anchoring to the start.
PORTAL_RE = re.compile(r"queryString:\s*'[^']*portal=(\d+)")
JOB_ID_RE = re.compile(r"[?&]job=([^&]+)")
# The colon is required, not optional -- "Location" appears as an
# ordinary word in plenty of description prose with no label intent
# ("No location label here"), and an optional colon matched that
# instead of skipping it.
LOCATION_LABEL_RE = re.compile(r"\b(?:Location|Primary Location)\s*:\s*([^\n<.]{2,80})", re.IGNORECASE)


class TaleoConnector(Connector):
    """Oracle Taleo's public candidate-facing RSS feed. No auth required.

    entry needs: {ats: taleo, company: "Display Name", tenant: "wipo",
                  section: "wp_internship", category: "..."}
    `tenant` and `section` come from the company's own board URL:
    https://<tenant>.taleo.net/careersection/<section>/jobsearch.ftl

    This only covers Taleo instances hosted at a guessable
    `<tenant>.taleo.net` -- a real, common pattern (confirmed live: WIPO,
    NATO). Many larger Taleo deployments are Oracle-hosted at an opaque
    `fa###.taleo.net` host unrelated to the company name (confirmed live
    via search results, e.g. fa009/fa007.taleo.net) -- same structural
    gap as oracle_recruiting.py's opaque per-company host, and not
    fixable by guessing.

    A second, separate gap (confirmed live on NATO's own `<tenant>.
    taleo.net` board, tenant/section both correct): some Taleo templates
    don't expose the RSS feed's portal id on the bare landing page at
    all -- no `queryString` JS variable, no RSS link in the DOM until an
    actual search has been run first. This connector's fetch() will
    raise "couldn't find a portal id" for a tenant like that even with a
    perfectly correct `tenant`/`section` -- not fixable from this page
    alone; would need to simulate an actual search POST first, not
    attempted here since WIPO's simpler template covers the case this
    was built for.

    The RSS feed carries title/link/description/pubDate but NOT a
    structured location field -- confirmed live (WIPO). This makes a
    best-effort attempt to pull a location out of the per-job detail
    page's text via LOCATION_LABEL_RE; when that doesn't match, location
    is left blank rather than guessed (see base.Posting's own note on
    why a blank beats a wrong guess).
    """

    name = "taleo"

    def fetch(self, entry: dict) -> list[Posting]:
        tenant = entry.get("tenant")
        section = entry.get("section")
        if not tenant or not section:
            raise ValueError(f"taleo entry for {entry.get('company')} needs both 'tenant' and 'section'")

        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
        page_resp = requests.get(JOBSEARCH_URL.format(tenant=tenant, section=section), headers=headers, timeout=20)
        if page_resp.status_code != 200 or "Career Section Unavailable" in page_resp.text:
            raise ValueError(
                f"taleo tenant='{tenant}' section='{section}' for {entry.get('company')} "
                "isn't a live career section — check tenant/section against the live careers page"
            )
        portal_match = PORTAL_RE.search(page_resp.text)
        if not portal_match:
            raise ValueError(
                f"couldn't find a portal id on taleo tenant='{tenant}' section='{section}' "
                f"for {entry.get('company')} — the page layout may have changed"
            )
        portal = portal_match.group(1)

        rss_resp = requests.get(
            RSS_URL.format(tenant=tenant, portal=portal), headers=headers, timeout=20
        )
        rss_resp.raise_for_status()
        try:
            root = ET.fromstring(rss_resp.content)
        except ET.ParseError as exc:
            raise ValueError(f"taleo RSS feed for {entry.get('company')} didn't parse as XML: {exc}") from exc

        max_pages = entry.get("max_pages", 60)
        postings = []
        for item in root.findall(".//item")[:max_pages]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            description_raw = item.findtext("description") or ""
            pub_date = item.findtext("pubDate")
            if not title or not link:
                continue

            job_id_match = JOB_ID_RE.search(link)
            job_id = job_id_match.group(1) if job_id_match else link

            location = ""
            loc_match = LOCATION_LABEL_RE.search(description_raw)
            if loc_match:
                location = loc_match.group(1).strip()

            postings.append(
                Posting(
                    id=f"taleo:{tenant}:{job_id}",
                    company=entry.get("company", tenant),
                    title=title,
                    location=location,
                    url=link,
                    source="taleo",
                    category=entry.get("category", ""),
                    posted_at=pub_date,
                    description_snippet=strip_html(description_raw),
                    description=to_display_text(description_raw),
                )
            )
        return postings
