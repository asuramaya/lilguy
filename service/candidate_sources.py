"""Free, public, no-API-key sources of real company NAMES to feed
discovery.py's candidate queue -- the actual bottleneck this project's
"self-updating source list" had, since the discovery LOGIC (probe -> real
trial fetch -> verification gate -> two-strike promotion) was proven
working weeks before this file existed, but only had 9 hand-typed names
to try. This is deliberately about WHICH companies to check, not HOW to
check them -- that distinction matters, see discovery.py's own docstring.

Researched and rejected, worth recording so a future session doesn't
re-try them:
  - SEC EDGAR's SIC-code company search (www.sec.gov/cgi-bin/browse-edgar
    ?action=getcompany&SIC=...) would let candidates be industry-
    prioritized (transportation/wholesale/manufacturing SIC ranges) --
    but its own atom output has a live SEC-side bug: entries render as
    literal `title="ARRAY(0x...)"` / `name="ARRAY(0x...)"` placeholders
    instead of the actual company name (a broken Perl/PHP-style template
    on SEC's end, not a parsing mistake on ours -- confirmed by fetching
    it directly). It also started failing outright (connection
    timeouts/HTML instead of atom) under light, paced, repeated use,
    while data.sec.gov's modern REST endpoints stayed solid throughout
    the same session. Not worth building a dependency on a legacy
    endpoint this fragile for a "nice to have" priority ordering when
    the underlying discovery/verification pipeline already treats every
    candidate the same way regardless of order.
  - NAICS-code-filterable company-NAME-level directories (which would
    give the same industry-prioritization win) are genuinely paywalled
    everywhere checked (Census only publishes aggregate establishment
    counts, never names; real company-by-NAICS directories are paid
    products from Data Axle/ZoomInfo/D&B) -- confirmed, not a gap in our
    research.

So: no industry filter for now, full breadth instead. This is actually
consistent with the project's own sourcing/filtering split (see
docs/sourcing-model.md) -- sourcing was always meant to be domain-
unfiltered by design, with relevance handled downstream by
filters.yaml, not by narrowing what gets sourced in the first place.
"""
import json
import re
import time

import requests

# SEC's fair-access policy actively rejects requests that don't look
# like a real descriptive identifier -- confirmed live: a User-Agent
# with a literal unsubstituted "<fork>" placeholder got a 403, while an
# otherwise-identical plain string got 200. Not a rate limit, a WAF
# content check on the header itself.
TIMEOUT = 20
UA = {"User-Agent": "internships-oss-project contact@example.org"}

SEC_EDGAR_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def fetch_sec_edgar_company_names() -> list[str]:
    """Every company SEC EDGAR has a ticker on file for -- 10,391 real
    names confirmed live, one HTTP GET, no key, no signup. SEC's own fair-
    access guidance asks for a descriptive User-Agent identifying the
    requester (a courtesy convention, not a paywall or auth mechanism).

    Coverage caveat, stated plainly: this only reaches PUBLICLY TRADED
    US companies. Many large private logistics/industrial employers
    (a lot of trucking/freight carriers, for instance) won't be in here
    at all -- this is one input to the candidate pool, not the only one
    this project should ever use.
    """
    resp = requests.get(SEC_EDGAR_COMPANY_TICKERS_URL, headers=UA, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    # Keys are stringified indices ("0", "1", ...), not meaningful -- the
    # actual records are the values.
    return [row["title"] for row in data.values() if row.get("title")]


WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"

# Verified live, one at a time, via the categorymembers API -- NOT
# guessed article/category titles. Guessing was tried and burned time
# earlier this project (docs/service-architecture.md's own history):
# "List of trucking companies"-style titles 404 more often than they
# hit, because Wikipedia's actual category names don't always match the
# obvious phrasing (there is no "Category:American manufacturing
# companies" or "Category:Freight forwarders" -- the real names are
# "Manufacturing companies of the United States" and "Freight transport
# companies", found via the API's own list=allcategories prefix search,
# not by guessing harder). Every category below returned real, current
# company articles when checked (13 to 196 members each) -- private
# carriers like AAA Cooper Transportation and Averitt Express among
# them, exactly the gap SEC EDGAR's public-companies-only list leaves.
# Checked and confirmed EMPTY/nonexistent under the obvious names, so a
# future session doesn't re-try them: "Third-party logistics companies",
# "Package delivery companies", "Companies in the railroad industry of
# the United States", "Warehousing companies", "Supply chain management
# companies", "Railroads of the United States", "Distribution (business)",
# "Intermodal freight transport companies", "Freight forwarders",
# "Warehousing", "Food distribution companies of the United States" --
# 0 members each, likely just not real category titles (Wikipedia may
# cover these companies under a different category entirely) rather than
# empty categories.
#
# Second pass (task #14, growing candidate breadth beyond the original
# logistics-only set): every industry below runs a real physical supply
# chain and is a plausible source of ops/logistics/supply-chain intern
# roles even though the company's primary industry label isn't
# "logistics" itself -- an aerospace or automotive manufacturer, a
# pharma company, a miner, all need the same warehousing/procurement/
# fulfillment functions this fork's default filter targets. Member
# counts confirmed live via the categorymembers API before adding:
# Cargo airlines (59), Aerospace (276), Automotive (53), Retail (161),
# Pharmaceutical (230), Chemical (193), Mining (62).
RELEVANT_WIKIPEDIA_CATEGORIES = [
    "Trucking companies of the United States",
    "Logistics companies of the United States",
    "Logistics companies",
    "Manufacturing companies of the United States",
    "Freight transport companies",
    "Shipping companies of the United States",
    "Defense companies of the United States",
    "Cargo airlines of the United States",
    "Aerospace companies of the United States",
    "Automotive companies of the United States",
    "Retail companies of the United States",
    "Pharmaceutical companies of the United States",
    "Chemical companies of the United States",
    "Mining companies of the United States",
]


def fetch_wikipedia_category_companies(categories: list[str] = None) -> list[str]:
    """Company names from Wikipedia category listings via the MediaWiki
    `list=categorymembers` API -- a structured, paginated, free, no-key
    endpoint, not a scrape of rendered category-page HTML. This is what
    makes it reliable where guessing article/category titles wasn't (see
    module-level comment on RELEVANT_WIKIPEDIA_CATEGORIES): the API
    either returns real members of a real category, or an empty list for
    a category that doesn't exist by that exact name -- no HTML to
    misparse, no guessing whether a 404 means "no such page" or
    "temporary error."

    Defaults to RELEVANT_WIKIPEDIA_CATEGORIES; pass your own list to
    pull a different set (e.g. a fork focused on a different industry).
    Wikipedia article titles sometimes carry a disambiguating suffix
    like "(company)" -- left as-is rather than stripped, since
    discovery.py's own probes normalize names into ATS-guess slugs
    anyway and a literal suffix doing no harm there beats silently
    mangling a title that needed it to stay unambiguous.
    """
    categories = categories if categories is not None else RELEVANT_WIKIPEDIA_CATEGORIES
    names: list[str] = []
    for category in categories:
        cmcontinue = None
        while True:
            params = {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": f"Category:{category}",
                "cmlimit": 500,
                "cmnamespace": 0,  # articles only -- excludes the category's own subcategory/talk pages
                "format": "json",
            }
            if cmcontinue:
                params["cmcontinue"] = cmcontinue
            resp = requests.get(WIKIPEDIA_API_URL, params=params, headers=UA, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            # "List of ..." articles are Wikipedia's own convention for an
            # INDEX page, not a company -- `cmnamespace=0` alone doesn't
            # exclude them (they're real articles, just not company
            # articles), and a company category can genuinely contain one
            # (confirmed live: "List of biotech and pharmaceutical
            # companies in the New York metropolitan area" sits inside
            # Category:Pharmaceutical companies). Slugifying a title like
            # that produces a 70+ character domain guess that isn't even
            # a valid DNS label -- confirmed live, this crashed
            # discovery.py's whole loop into a restart crash-loop before
            # both this filter and discovery.py's own broader exception
            # handling were added (see discovery.py's _probe_jsonld note).
            names.extend(
                m["title"] for m in data.get("query", {}).get("categorymembers", [])
                if m.get("title") and not m["title"].lower().startswith("list of")
            )
            cmcontinue = data.get("continue", {}).get("cmcontinue")
            if not cmcontinue:
                break
    return names


# Common Crawl's free CDX index (no key, no signup) lets a URL PATTERN be
# queried directly against real, currently-crawled pages -- inverting
# discovery.py's whole model for two of its four ATS platforms. Instead of
# guessing "does {slugified company name} exist on Greenhouse/Workday" one
# candidate at a time, this asks Common Crawl "what does it actually have
# under boards.greenhouse.io/* / *.myworkdayjobs.com/*" and gets back real,
# currently-live company tokens and Workday tenant/host/site triples
# directly -- no guessing involved for anything this returns.
#
# Confirmed live (this session): boards.greenhouse.io + job-boards.
# greenhouse.io together yielded 1,946 distinct real company tokens in
# about 20 seconds. *.myworkdayjobs.com yielded 346 distinct tenant/host
# pairs from just the first of 5 total pages -- a full pull is easily
# 1,000+, each with a real `site` path segment recoverable too (e.g. 3M's
# real site value is literally "Search", not one of the three site names
# discovery.py already guesses).
#
# Deliberately does NOT cover Lever: jobs.lever.co/robots.txt explicitly
# disallows CCBot ("User-agent: CCBot / Disallow: /"), so Common Crawl has
# almost nothing indexed there (confirmed live -- the CDX query returns
# a handful of robots.txt hits and nothing else). That's a real, permanent
# constraint of this approach for Lever specifically, not a gap to close
# with more querying -- Lever stays on the existing guess-based probe.
COMMONCRAWL_COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"
COMMONCRAWL_TIMEOUT = 60  # a full CDX page can be several MB and take longer than the 20s default

# A locale/language path segment (Workday puts these first for
# internationalized boards, e.g. .../en-US/Search/job/... or .../de-DE/
# Search) -- not a real "site" name, skip it when looking for the site
# segment. Matches "en", "en-US", "de-DE", "pt-BR", etc.
_LOCALE_SEGMENT_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,4})?$")


def _latest_commoncrawl_index() -> str:
    resp = requests.get(COMMONCRAWL_COLLINFO_URL, headers=UA, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()[0]["id"]  # most recent index is always first


def _get_cdx_page_with_retry(base: str, url_pattern: str, page: int, attempts: int = 3):
    # A CDX page response can be several MB (confirmed live: the
    # *.myworkdayjobs.com pattern's pages ran multiple MB each) -- large
    # chunked responses over a real network transiently truncate
    # (confirmed live: requests.exceptions.ChunkedEncodingError,
    # "Response ended prematurely", on an otherwise-correct request from
    # inside the actual discovery container, not a local mock). Same
    # retry shape as jsonld.py's _get_sitemap_with_retry -- one bad
    # request shouldn't lose an entire page of real candidates when a
    # short retry would likely succeed.
    last_exc = None
    for attempt in range(attempts):
        try:
            resp = requests.get(base, params={"url": url_pattern, "output": "json", "page": page},
                                 headers=UA, timeout=COMMONCRAWL_TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last_exc


def _fetch_commoncrawl_cdx_urls(url_pattern: str, index: str) -> list[str]:
    """Every URL Common Crawl's CDX index has for a pattern like
    'boards.greenhouse.io/*'. Paginates via CDX's own page mechanism --
    confirmed live a single page can already be tens of thousands of rows
    and the real page count varies a lot by pattern (1 page for the
    Greenhouse domains tested, 5 for *.myworkdayjobs.com), so this always
    checks showNumPages rather than assuming either shape.
    """
    base = f"https://index.commoncrawl.org/{index}-index"
    meta = requests.get(base, params={"url": url_pattern, "output": "json", "showNumPages": "true"},
                         headers=UA, timeout=TIMEOUT).json()
    num_pages = meta.get("pages", 1)
    urls: list[str] = []
    for page in range(num_pages):
        resp = _get_cdx_page_with_retry(base, url_pattern, page)
        for line in resp.text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("url"):
                urls.append(row["url"])
    return urls


_SKIP_PATH_TOKENS = ("robots.txt", "favicon.ico")


def fetch_commoncrawl_path_tokens(domains: list[str], lowercase: bool = True, index: str = None) -> list[str]:
    """Generic first-path-segment token fetcher -- the shared shape
    behind every ATS below whose board URL is `{domain}/{token}/...`:
    Greenhouse, Ashby, SmartRecruiters, Workable all look like this.
    Each token is a company that verifiably has a board there already,
    not a guess -- and every probe in discovery.py that consumes these
    already slugifies (or, for a case-sensitive one, tries the token
    as-is) before querying, which is a no-op on an already-slug-shaped
    token, so these feed in as plain candidate names with no special
    handling on the discovery.py side.

    `domains` can list more than one host -- Greenhouse migrated its
    canonical domain at some point and Common Crawl has real, current
    coverage of both (confirmed live: the older domain 301-redirects to
    the newer one, but plenty of pages are still indexed under it
    directly). Most ATS platforms only need one.

    The trailing path segments that are not board tokens are dropped
    explicitly: an application URL is often `/{token}/{job-id}/apply`,
    so taking the FIRST segment is what identifies the board.
    """
    index = index or _latest_commoncrawl_index()
    tokens: set[str] = set()
    for domain in domains:
        marker = f"{domain}/"
        for url in _fetch_commoncrawl_cdx_urls(f"{domain}/*", index):
            if marker not in url:
                continue
            token = url.split(marker, 1)[1].split("/", 1)[0].split("?", 1)[0]
            if lowercase:
                token = token.lower()
            if token and token.lower() not in _SKIP_PATH_TOKENS:
                tokens.add(token)
    return sorted(tokens)


def fetch_commoncrawl_subdomain_tokens(url_pattern: str, host_re: re.Pattern, index: str = None) -> list[str]:
    """Generic token fetcher for an ATS whose board lives at a variable
    SUBDOMAIN rather than a fixed domain plus a path token -- confirmed
    shape: iCIMS (careers-{slug}.icims.com), BambooHR ({slug}.bamboohr.
    com). `host_re` must have exactly one capture group around the slug;
    matched against the full URL so it's free to anchor on a literal
    prefix inside the hostname (e.g. "careers-") that CDX's own pattern
    syntax can't express as a mid-hostname wildcard -- `url_pattern`
    is deliberately broader than that prefix for exactly this reason,
    filtered down here instead.
    """
    index = index or _latest_commoncrawl_index()
    tokens: set[str] = set()
    for url in _fetch_commoncrawl_cdx_urls(url_pattern, index):
        m = host_re.search(url)
        if m:
            tokens.add(m.group(1).lower())
    return sorted(tokens)


def fetch_commoncrawl_greenhouse_tokens(index: str = None) -> list[str]:
    """Real, currently-live Greenhouse board tokens. Queries BOTH
    boards.greenhouse.io and job-boards.greenhouse.io -- Greenhouse
    migrated their canonical domain at some point and Common Crawl has
    real, current coverage of both. See fetch_commoncrawl_path_tokens
    for the shared mechanics."""
    return fetch_commoncrawl_path_tokens(
        ["boards.greenhouse.io", "job-boards.greenhouse.io"], lowercase=True, index=index
    )


def fetch_commoncrawl_ashby_tokens(index: str = None) -> list[str]:
    """Real, currently-live Ashby board slugs (jobs.ashbyhq.com/{slug})."""
    return fetch_commoncrawl_path_tokens(["jobs.ashbyhq.com"], lowercase=True, index=index)


def fetch_commoncrawl_smartrecruiters_tokens(index: str = None) -> list[str]:
    """Real, currently-live SmartRecruiters company identifiers
    (jobs.smartrecruiters.com/{Identifier}).

    Case is PRESERVED here, unlike every other path-token fetcher,
    because SmartRecruiters' identifier is case-sensitive and lowercasing
    it produces a token that returns an empty list rather than a 404 --
    a miss that looks exactly like a company with no openings.
    """
    return fetch_commoncrawl_path_tokens(["jobs.smartrecruiters.com"], lowercase=False, index=index)


def fetch_commoncrawl_workable_tokens(index: str = None) -> list[str]:
    """Real, currently-live Workable account tokens
    (apply.workable.com/{token}/). Fixes a real gap the guessing-only
    _probe_workable() has: Workable tokens are often hyphenated
    ("back-market") rather than the bare company slug, and there's no
    way to guess that from a company name alone -- these come from
    verifiably real, currently-live boards instead."""
    return fetch_commoncrawl_path_tokens(["apply.workable.com"], lowercase=True, index=index)


ICIMS_HOST_RE = re.compile(r"https?://careers-([a-z0-9-]+)\.icims\.com", re.IGNORECASE)
BAMBOOHR_HOST_RE = re.compile(r"https?://([a-z0-9-]+)\.bamboohr\.com", re.IGNORECASE)


def fetch_commoncrawl_icims_slugs(index: str = None) -> list[str]:
    """Real, currently-live iCIMS board slugs (careers-{slug}.icims.com).

    Fixes a real, significant gap the guessing-only _probe_icims() has:
    confirmed live via search results that many real iCIMS slugs are NOT
    derived from the company name at all (In-Q-Tel's is "iqt", one
    observed board is bare "mdi", another "professionalservices") -- a
    company-name guess structurally cannot find these. `*.icims.com/*`
    is queried broadly (CDX can't wildcard a literal prefix like
    "careers-" in the middle of a hostname) and filtered down to the
    "careers-" ones by ICIMS_HOST_RE.
    """
    return fetch_commoncrawl_subdomain_tokens("*.icims.com/*", ICIMS_HOST_RE, index=index)


def fetch_commoncrawl_bamboohr_tokens(index: str = None) -> list[str]:
    """Real, currently-live BambooHR account tokens
    ({token}.bamboohr.com/careers)."""
    return fetch_commoncrawl_subdomain_tokens("*.bamboohr.com/careers*", BAMBOOHR_HOST_RE, index=index)


def fetch_commoncrawl_workday_tenants(index: str = None) -> list[dict]:
    """Real (tenant, wd_host, site) triples straight from Common Crawl --
    a fully-formed Workday config, not a guess to feed into discovery.py's
    WORKDAY_HOST_GUESSES/WORKDAY_SITE_GUESSES matrix. For each distinct
    tenant.host pair seen, picks the most common non-locale path segment
    as `site` (a board can have multiple real site names across different
    URLs -- e.g. a locale-specific one and a generic one -- so this isn't
    guaranteed to be the "canonical" one, but it's a real, confirmed-live
    value either way, which is what matters for a trial fetch).

    Returns dicts shaped for discovery.py to seed directly as a resolved
    (ats, config) pair rather than a plain candidate name -- these skip
    the guess-probe step entirely, going straight to a real trial fetch.
    """
    index = index or _latest_commoncrawl_index()
    # tenant_host -> {site_value: count}
    site_votes: dict[str, dict[str, int]] = {}
    for url in _fetch_commoncrawl_cdx_urls("*.myworkdayjobs.com/*", index):
        m = re.match(r"https?://([a-z0-9-]+)\.([a-z0-9]+)\.myworkdayjobs\.com/([^/?]*)", url, re.IGNORECASE)
        if not m:
            continue
        tenant, host, first_segment = m.group(1).lower(), m.group(2).lower(), m.group(3)
        if not first_segment or first_segment.lower() == "robots.txt" or _LOCALE_SEGMENT_RE.match(first_segment):
            continue
        key = f"{tenant}|{host}"
        votes = site_votes.setdefault(key, {})
        votes[first_segment] = votes.get(first_segment, 0) + 1

    results = []
    for key, votes in site_votes.items():
        tenant, host = key.split("|", 1)
        best_site = max(votes.items(), key=lambda kv: kv[1])[0]
        results.append({"tenant": tenant, "wd_host": host, "site": best_site})
    return results


TALEO_URL_RE = re.compile(
    r"https?://([a-z0-9-]+)\.taleo\.net/careersection/([^/]+)/jobsearch\.ftl", re.IGNORECASE
)


def fetch_commoncrawl_taleo_tenants(index: str = None) -> list[dict]:
    """Real (tenant, section) pairs straight from Common Crawl -- sidesteps
    _probe_taleo()'s guessing-based TALEO_SECTION_GUESSES entirely (see
    that function's own docstring: section slugs are often company-chosen
    and not derivable at all, confirmed live on WIPO's "wp_internship").
    Same shape and same "most commonly observed value wins" voting as
    fetch_commoncrawl_workday_tenants above -- a tenant can have more than
    one real section crawled (e.g. a general one and a locale- or
    department-specific one).

    Doesn't resolve the RSS feed's portal id -- that's extracted at
    connector fetch() time from the live page (see taleo.py), not
    knowable from a crawled URL alone.
    """
    index = index or _latest_commoncrawl_index()
    section_votes: dict[str, dict[str, int]] = {}
    for url in _fetch_commoncrawl_cdx_urls("*.taleo.net/careersection/*/jobsearch.ftl*", index):
        m = TALEO_URL_RE.match(url)
        if not m:
            continue
        tenant, section = m.group(1).lower(), m.group(2)
        votes = section_votes.setdefault(tenant, {})
        votes[section] = votes.get(section, 0) + 1

    results = []
    for tenant, votes in section_votes.items():
        best_section = max(votes.items(), key=lambda kv: kv[1])[0]
        results.append({"tenant": tenant, "section": best_section})
    return results


UKG_URL_RE = re.compile(
    r"https?://(recruiting2?\.ultipro\.com)/([a-z0-9]+)/JobBoard/([0-9a-f-]{36})", re.IGNORECASE
)


def fetch_commoncrawl_ukg_boards(index: str = None) -> list[dict]:
    """Real (host, tenant, board_id) triples for UKG Pro Recruiting
    (formerly UltiPro) boards, straight from Common Crawl.

    Unlike Workday's `site` or Taleo's `section`, there's nothing to vote
    on here -- a crawled URL's board_id IS the canonical, unique board
    identifier for that tenant (an opaque GUID, not a company-chosen
    label with multiple real variants), so every match is kept rather
    than reduced to "most common."

    Queries both recruiting.ultipro.com and recruiting2.ultipro.com --
    confirmed live, UKG splits real tenants across both.
    """
    index = index or _latest_commoncrawl_index()
    seen: set[tuple[str, str, str]] = set()
    results = []
    for host_pattern in ("recruiting.ultipro.com/*/JobBoard/*", "recruiting2.ultipro.com/*/JobBoard/*"):
        for url in _fetch_commoncrawl_cdx_urls(host_pattern, index):
            m = UKG_URL_RE.match(url)
            if not m:
                continue
            host, tenant, board_id = m.group(1).lower(), m.group(2), m.group(3).lower()
            key = (host, tenant.lower(), board_id)
            if key in seen:
                continue
            seen.add(key)
            results.append({"host": host, "tenant": tenant, "board_id": board_id})
    return results
