#!/usr/bin/env python3
"""Semi-automate the "what ATS does this company use" step from
docs/adding-a-source.md, so adding a source stops being manual archaeology
every time.

This is honest about its limits, not magic: a careers site that's fully
JS-rendered (job data loaded client-side, no server-side JSON-LD, no
per-job sitemap) can't be discovered by plain HTTP requests — that's how
this project found Honeywell (Oracle Recruiting Cloud) and Unilever
(Workday) in the first place: a browser session reading real network
requests, or a targeted web search for a known myworkdayjobs.com tenant.
When this tool comes up empty, that's the honest answer, not a bug to
work around by guessing.

Usage:
  python scraper/discover.py example.com
  python scraper/discover.py --company "Example Corp" careers.example.com

What it checks, in order:
  1. Greenhouse / Lever token guesses derived from the company name.
  2. robots.txt for a Sitemap: line (following one level of sitemap-index
     nesting), then samples job-shaped URLs from what it finds and checks
     each for schema.org/JobPosting — as JSON-LD (the jsonld.py connector
     supports this) or as microdata (noted but NOT yet supported by any
     connector here — see sources.yaml's J.B. Hunt comment for why).
"""
import argparse
import re
import sys
from urllib.parse import urljoin, urlparse

import requests

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
LEVER_API = "https://api.lever.co/v0/postings/{token}?mode=json"
JOB_URL_HINTS = re.compile(r"/(job|jobs|position|req|career)s?[/_-]", re.IGNORECASE)
LD_JSON_RE = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.DOTALL)
MICRODATA_RE = re.compile(r'itemtype=["\'][^"\']*schema\.org/JobPosting["\']', re.IGNORECASE)

TIMEOUT = 12
UA = {"User-Agent": "Mozilla/5.0 (internship-feed-bot; source discovery)"}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def check_greenhouse(token: str):
    try:
        r = requests.get(GREENHOUSE_API.format(token=token), timeout=TIMEOUT, headers=UA)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        return len(r.json().get("jobs", []))
    except ValueError:
        return None


def check_lever(token: str):
    try:
        r = requests.get(LEVER_API.format(token=token), timeout=TIMEOUT, headers=UA)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
        return len(data) if isinstance(data, list) else None
    except ValueError:
        return None


def get_sitemap_urls(domain: str) -> list[str]:
    try:
        r = requests.get(f"https://{domain}/robots.txt", timeout=TIMEOUT, headers=UA)
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    return re.findall(r"^\s*sitemap:\s*(\S+)", r.text, re.IGNORECASE | re.MULTILINE)


def fetch_sitemap_locs(sitemap_url: str) -> list[str]:
    try:
        r = requests.get(sitemap_url, timeout=TIMEOUT, headers=UA)
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    return re.findall(r"<loc>([^<]+)</loc>", r.text)


def find_job_sitemap_candidates(domain: str) -> list[str]:
    """Returns individual URLs that look like they might be job postings,
    from whichever sitemap(s) robots.txt advertises (expanding one level
    of sitemap-index nesting)."""
    top_level = get_sitemap_urls(domain)
    if not top_level:
        return []

    job_like = []
    for sm in top_level[:10]:  # don't chase an unbounded number of regional sitemaps
        locs = fetch_sitemap_locs(sm)
        # a sitemap-index has <loc> entries pointing at more sitemaps, not
        # pages — cheap heuristic: those end in .xml
        sub_sitemaps = [loc for loc in locs if loc.endswith(".xml")]
        if sub_sitemaps and len(sub_sitemaps) == len(locs):
            for sub in sub_sitemaps[:5]:
                locs = fetch_sitemap_locs(sub)
                job_like.extend(loc for loc in locs if JOB_URL_HINTS.search(urlparse(loc).path))
        else:
            job_like.extend(loc for loc in locs if JOB_URL_HINTS.search(urlparse(loc).path))
    return job_like


def check_job_posting_markup(url: str) -> str | None:
    """Returns 'jsonld', 'microdata', or None."""
    try:
        r = requests.get(url, timeout=TIMEOUT, headers=UA)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    for block in LD_JSON_RE.findall(r.text):
        if '"@type":"JobPosting"' in block.replace(" ", "") or '"@type": "JobPosting"' in block:
            return "jsonld"
    if MICRODATA_RE.search(r.text):
        return "microdata"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("domain", help="Company's careers domain, e.g. careers.example.com")
    parser.add_argument("--company", default=None, help="Display name, defaults to the domain")
    args = parser.parse_args()

    company = args.company or args.domain
    print(f"Checking {company} ({args.domain})...\n")

    slug = slugify(company)
    print("== Greenhouse / Lever ==")
    for name, token, checker, api_kind in [
        ("Greenhouse", slug, check_greenhouse, "greenhouse"),
        ("Lever", slug, check_lever, "lever"),
    ]:
        count = checker(token)
        if count is not None:
            print(f"  MATCH: {name} token '{token}' -> {count} job(s) currently posted")
            print(f"  sources.yaml entry:\n    - company: {company}\n      ats: {api_kind}\n      token: {token}\n      category: <fill in>")
        else:
            print(f"  no match for token '{slug}'")

    print("\n== Job sitemap (Tier 1.5 / jsonld) ==")
    candidates = find_job_sitemap_candidates(args.domain)
    if not candidates:
        print(f"  No sitemap advertised in https://{args.domain}/robots.txt, or none of its "
              "entries look job-shaped. Doesn't mean there's no sitemap — try the company's "
              "actual careers subdomain if this domain isn't it (e.g. careers.<domain>, "
              "jobs.<domain>) by re-running with that domain.")
    else:
        print(f"  Found {len(candidates)} job-shaped URL(s). Checking up to 3 for JobPosting markup...")
        found_any = False
        for url in candidates[:3]:
            markup = check_job_posting_markup(url)
            if markup == "jsonld":
                print(f"  MATCH (JSON-LD, supported by scraper/connectors/jsonld.py): {url}")
                found_any = True
            elif markup == "microdata":
                print(f"  Found microdata JobPosting (NOT YET supported by any connector here): {url}")
                found_any = True
            else:
                print(f"  no JobPosting markup found on: {url}")
        if not found_any:
            print("  None of the sampled URLs had JobPosting markup this tool recognizes.")

    print(
        "\nNo match anywhere above means the careers site is likely fully "
        "JS-rendered (Workday, Oracle Recruiting Cloud, SuccessFactors, "
        "iCIMS, Taleo, or a custom SPA) — see docs/adding-a-source.md for "
        "how to find those with a browser session instead."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
