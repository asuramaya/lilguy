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
# company articles when checked (66 to 131 members each) -- private
# carriers like AAA Cooper Transportation and Averitt Express among
# them, exactly the gap SEC EDGAR's public-companies-only list leaves.
RELEVANT_WIKIPEDIA_CATEGORIES = [
    "Trucking companies of the United States",
    "Logistics companies of the United States",
    "Logistics companies",
    "Manufacturing companies of the United States",
    "Freight transport companies",
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
            names.extend(m["title"] for m in data.get("query", {}).get("categorymembers", []) if m.get("title"))
            cmcontinue = data.get("continue", {}).get("cmcontinue")
            if not cmcontinue:
                break
    return names
