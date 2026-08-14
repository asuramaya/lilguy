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
