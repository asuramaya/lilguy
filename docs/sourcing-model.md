# Why three tiers, not one hardcoded list

The first version of this scraper was one connector per named company:
add Caterpillar, add Cummins, add P&G, keep going. That doesn't scale and
it's the wrong shape: it caps coverage at however many companies someone
manually wires up, and it goes stale the moment a company not on the list
starts hiring.

## Prior art

Looked at how existing open-source internship trackers actually source
data before building further:

- **[SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships)**,
  the best-known one. Combination of automated scraping across "top tech
  companies and startups" plus community PRs adding roles by hand. Scale
  comes from crowdsourcing, not from a bigger hardcoded list. That's a model this
  project can't replicate solo, but worth knowing it's the alternative if
  this ever wants a community-contribution path.
- **[datascry/openroles](https://github.com/datascry/openroles)**: "Open
  roles across 52 hiring platforms," no backend. The actually relevant
  architecture: a JSON-LD harvester that reads **schema.org/JobPosting**
  structured data directly off company career pages (the same markup
  employers add so Google for Jobs can index them, an existing standard,
  not something this project invented), plus a Google-for-Jobs RSS
  fallback for sites that block direct scraping, plus one-off custom
  adapters only for a handful of outlier big-name sites.

## What this project uses

Three tiers, in `sources.yaml`, in the order to reach for them:

**Tier 1, aggregators.** One entry, many companies, no list to maintain.
Currently: **The Muse's public API** (`scraper/connectors/muse.py`), which
needs no key and already indexes postings across companies never
hand-added here. Confirmed live pulling real Jabil, Hitachi Energy, BD,
GE Vernova, and Walmart internships with zero per-company setup. Queried
**per category, not one blended query**: confirmed live these are NOT
equivalent once a category's real total exceeds the API's own pagination
depth limit (~1900 results). "Healthcare" alone has ~3500 internship-level
postings, so a single blended query's first ~1900 results skewed
overwhelmingly Healthcare and starved every smaller category (e.g.
Transportation and Logistics showed 12 postings blended vs. 69 queried on
its own). `sources.yaml`'s Muse entry lists ~33 real category names,
found by querying each one directly and keeping every one with a nonzero
total. Domain relevance is still a `filters.yaml` question, not a
sourcing one (see below); this is purely about not starving smaller
categories of their fair share of the depth-limited query budget. The
depth limit itself (`max_pages: 95`, the API returns HTTP 400 past
roughly page 96-99 of ANY query) is a hard ceiling, not a politeness
setting. The Muse is the only aggregator this project ships. Adzuna and
USAJobs connectors existed until 2026-08 and were removed: both required
API keys, which this project's standing rule excludes, and both had zero
live sources; neither had ever run. Every connector here reads a public,
keyless endpoint, which is what lets a fork work without signing up for
anything.

**Ashby** (`scraper/connectors/ashby.py`) and **SmartRecruiters**
(`scraper/connectors/smartrecruiters.py`) are the newest direct-board
connectors, both keyless. Ashby covers the funded-startup segment the
other connectors miss; SmartRecruiters skews large-enterprise and
European, so the two barely overlap.

Two things worth knowing before adding sources on either:

Ashby's LIST response already carries `descriptionPlain` and
`descriptionHtml`, so descriptions arrive free. Confirmed live at
136/136 postings on one board. That is not a small detail. Workday's
list response carries none, which forces a per-posting fetch, which is
where the description-backfill deadlock came from.

SmartRecruiters is the opposite: its list carries no description at all,
so this connector leaves the field unset (stored NULL, "not fetched
yet") rather than putting an N+1 fetch on the scrape path. It also
reports a per-posting `function` label, which is the first source of
job-function values on a direct board. Its `industry` label is
deliberately NOT used: that is SmartRecruiters' own vocabulary, and
writing it into `category` would reintroduce a third taxonomy into the
column two of them were just disentangled from.

Its company identifier is **case-sensitive** ("Visa", not "visa"), and a
wrong case returns an empty list rather than a 404, so a typo looks
exactly like a company with no openings. The Common Crawl fetcher for it
preserves case for this reason, unlike every other fetcher in that
module.

**Tier 1.5, generic schema.org/JobPosting harvester**
(`scraper/connectors/jsonld.py`), openroles' actual technique: read a
company's job sitemap for individual posting URLs, fetch each, and pull
the `JobPosting` block out of its embedded JSON-LD. Still one entry per
company (needs that company's sitemap URL), but the *connector code* is
generic: it doesn't care which ATS vendor is underneath, unlike
Greenhouse/Lever/Workday/Oracle Recruiting Cloud, which each need their own connector class.
Confirmed live on UPS's Phenom-People-hosted career site, an ATS none of
this project's other connectors support, including a real "Seasonal HR
Intern" posting. This is the thing to reach for first when adding a new
company that isn't on Greenhouse/Lever/Workday/Oracle Recruiting Cloud, before writing a new
vendor-specific connector: check `<company>/robots.txt` for a sitemap,
find the one listing individual job URLs (not just category/landing
pages; UPS publishes both, only one is useful), and see if a job page
has `<script type="application/ld+json">...JobPosting...`. Two more
things confirmed live building this out further. First, `sitemap_url` can be
a sitemap-INDEX (a sitemap of sitemaps) rather than a flat list of job
URLs: RTX splits its ~4500 job postings across 9 numbered sub-sitemaps
under one index, so `jsonld.py` auto-expands one level of index nesting
rather than requiring a specific sub-sitemap to be hand-picked. Second, firing
requests back-to-back with no pacing can trip a site's bot-detection even
when the User-Agent isn't the issue. RTX's WAF intermittently returned
403 under rapid sequential `requests` calls while the SAME URLs, SAME
headers, worked fine via interactively-run `curl` (which naturally has
pauses between calls); a small delay between every request
(`REQUEST_DELAY_SECONDS` in `jsonld.py`) eliminated it. Treat this as a
default expectation for any future Tier 1.5 source, not a one-off
workaround for RTX specifically.

**Tier 2: vendor-specific per-company connectors.** Still useful, not
replaced: highest precision for a company you're specifically targeting,
and a fallback for the (rare) company that neither an aggregator indexes
nor exposes a job sitemap. This is
`scraper/connectors/{greenhouse,lever,workday,oracle_recruiting}.py` plus
`docs/adding-a-source.md`. Reach for Tier 1.5 before writing a new one of
these; it only pays off when a company happens to already run
Greenhouse, Lever, Workday, or Oracle Recruiting Cloud.

**Oracle Recruiting Cloud** (`oracle_recruiting.py`) was the biggest
sourcing gap this project had: it powers Honeywell and is suspected
(never confirmed) for several other large industrials/CPG companies. It
has no public sitemap the `jsonld` connector could ever find, and unlike
Workday its host is an opaque per-company hash (Honeywell's is
`ibqbjb`), not a guessable subdomain; WebSearch, which found most of
this project's Workday tenants, doesn't surface Oracle Recruiting Cloud
hosts the same way. Finding it required an actual browser session
reading real network requests (`recruitingCEJobRequisitions` in the
DevTools Network tab), the same technique this project first used just
to confirm Honeywell WASN'T on Workday. Its API returns a `TotalJobsCount`
and an offset-paginated `requisitionList` with plain-text fields: no
HTML to strip, no schema.org markup involved at all, a genuinely
different JSON shape than every other connector here.

**A "custom" careers site can still be a thin wrapper over a real ATS
underneath**, worth checking before assuming a one-off connector is
needed. J.B. Hunt's careers.jbhunt.com has its own bespoke-looking API
(`ww5.jbhunt.com/api/careers/jobs/`, POST with an empty body), which
looked at first like it would need its own dedicated connector for a
platform nobody else uses. But that API's own response data included a
`jobPostingExternalUrl` field pointing at
`jbhunt.wd501.myworkdayjobs.com/...`: the real backend, underneath the
custom branding, was ordinary Workday. Used the existing `workday.py`
connector directly instead of writing a new one. The tell: look at what
URL a "custom" API's own data points candidates toward applying: a
company that built its own front-end skin still very often didn't build
its own ATS backend.

## Sourcing vs. filtering are two different questions, on purpose

Earlier versions of this project answered "what postings exist" and "which
ones do I want" in the same place: a source was scraped AND filtered to
ops/logistics/supply-chain in one step (`is_relevant()` in what was then
`filters.py`, with a per-source `domain_native` escape hatch). That
collapsed as soon as the project needed to serve more than one person's
interest: a marketing-track reader forking this repo would have inherited
a scraper that had already thrown their postings away before they ever
saw them.

The fix was to split the two concerns cleanly:

- `scraper/scrape.py` + `sources.yaml` answer ONLY "is this an internship
  posting" (title contains "intern"/"internship"/"co-op") and write
  everything that passes to `data/all_postings.json`: the complete,
  domain-unfiltered raw feed, every industry The Muse's `level=Internship`
  query covers plus every Tier 1.5/Tier 2 source.
- `filters.yaml` + `scraper/user_filter.py` + `scraper/build_feed.py`
  answer "which of those do I actually want," applied AFTER scraping,
  against the same shared raw store. This fork's own `filters.yaml` is
  ops/logistics/supply-chain by default, but it's config, not code: copy
  it, point `build_feed.py` at your copy, get your own feed with zero
  re-scraping and zero code changes.

## A source failing to fetch is not the same fact as a source reporting nothing

The most serious bug found building this project, not a minor one: a
single transient HTTP timeout (one bad request out of roughly 250 made
for a full Muse sweep) caused the ENTIRE Muse connector call to raise an
exception. `scrape.py`'s `fetch_all()` correctly excluded that source
from this run's fresh postings (as designed; one broken source shouldn't
corrupt what other sources return). The problem was one layer up:
`store.py`'s `rebuild()` treats "not present in this run's fresh
postings" as "this posting has closed", which is the CORRECT read when a
source successfully fetched and legitimately has fewer postings now, but
was silently and catastrophically wrong here, because Muse's absence was
due to a failed fetch, not a real change in what's posted. One network
blip took the raw store from ~4200 postings to 98 in a single run.

The fix, now in place: every `Posting` carries a `source_entry` field (set
by `scrape.py` after `fetch()` returns, not by the connector itself),
recording which `sources.yaml` entry produced it, distinct from `source` (the ATS
type, since several entries can share one). `fetch_all()` tracks which
entries raised this run. `rebuild()` takes that set and, for any entry
that failed, carries its previously-known postings forward UNCHANGED
rather than treating their absence as closure. A source that fails to
fetch now degrades to "stale until it succeeds again," never to "silently
erased."

A second, complementary fix addresses the Muse connector specifically,
since it's the source making by far the most individual requests (one per
category per page): each category's fetch is now retried on a transient
network error before giving up, and if a category still fails after
retries, only THAT category's contribution is skipped (the other ~32
keep going) rather than one bad category taking down the whole run's
Muse results. `MuseConnector.fetch()` only raises if literally every
category failed, preserving "loud failure over silent success" for a
genuinely broken config while no longer treating "loud" and "total" as
the same thing.

The general principle, worth carrying into anything added here later:
**a pipeline stage's own resilience (retry, partial-failure isolation)
and the DOWNSTREAM stage's ability to tell "fetch failed" apart from
"fetch succeeded and found nothing" are two different, both-necessary
defenses**: the first reduces how often failures happen, and the second
bounds the damage on whichever ones still do.

## A concrete lesson from building this

An aggregator's own category taxonomy is not the same thing as any
particular reader's relevance filter, and trusting it blindly is a
mistake, proven live, back when sourcing and filtering were still one
step: The Muse's "Business Operations" category (with `level=Internship`)
returned 160 postings, but a full 146 of them were hotel-administration
internships at Selina and social-media account-management internships at
TikTok, not anything industrial/supply-chain/logistics. A keyword check
on top of the aggregator's results (now living in `filters.yaml` rather
than baked into the Muse connector) cut that down with zero loss of the
real hits. See `filters.yaml`'s own comment on why bare "operations" was
dropped as a keyword (matched that same noise, added no genuine hits
beyond more specific terms already there).
