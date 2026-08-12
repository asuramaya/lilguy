# Why three tiers, not one hardcoded list

The first version of this scraper was one connector per named company —
add Caterpillar, add Cummins, add P&G, keep going. That doesn't scale and
it's the wrong shape: it caps coverage at however many companies someone
manually wires up, and it goes stale the moment a company not on the list
starts hiring.

## Prior art

Looked at how existing open-source internship trackers actually source
data before building further:

- **[SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships)**
  — the best-known one. Combination of automated scraping across "top tech
  companies and startups" plus community PRs adding roles by hand. Scale
  comes from crowdsourcing, not from a bigger hardcoded list — a model this
  project can't replicate solo, but worth knowing it's the alternative if
  this ever wants a community-contribution path.
- **[datascry/openroles](https://github.com/datascry/openroles)** — "Open
  roles across 52 hiring platforms," no backend. The actually relevant
  architecture: a JSON-LD harvester that reads **schema.org/JobPosting**
  structured data directly off company career pages (the same markup
  employers add so Google for Jobs can index them — an existing standard,
  not something this project invented), plus a Google-for-Jobs RSS
  fallback for sites that block direct scraping, plus one-off custom
  adapters only for a handful of outlier big-name sites.

## What this project uses

Three tiers, in `sources.yaml`, in the order to reach for them:

**Tier 1 — aggregators.** One entry, many companies, no list to maintain.
Currently: **The Muse's public API** (`scraper/connectors/muse.py`), which
needs no key and already indexes postings across companies never
hand-added here — confirmed live pulling real Jabil, Hitachi Energy, BD,
GE Vernova, and Walmart internships with zero per-company setup. As
configured in `sources.yaml` it pulls with NO category restriction —
every internship-level posting The Muse has indexed (~8000 postings,
every industry) rather than pre-narrowing to one category — because
domain relevance is now a `filters.yaml` question, not a sourcing one
(see below). One real constraint found running it at that scale: the API
itself starts returning HTTP 400 past roughly page 96-99 of any query
regardless of how many total results exist — an undocumented depth limit,
not a config error, which is why `max_pages: 95` in `sources.yaml` is a
hard ceiling, not a politeness setting. Adzuna (`scraper/connectors/adzuna.py`)
is wired up the same way as a second aggregator, gated behind a free API
key the repo doesn't ship (see the commented-out entry in `sources.yaml`).

**Tier 1.5 — generic schema.org/JobPosting harvester**
(`scraper/connectors/jsonld.py`), openroles' actual technique: read a
company's job sitemap for individual posting URLs, fetch each, and pull
the `JobPosting` block out of its embedded JSON-LD. Still one entry per
company (needs that company's sitemap URL), but the *connector code* is
generic — it doesn't care which ATS vendor is underneath, unlike
Greenhouse/Lever/Workday which each need their own connector class.
Confirmed live on UPS's Phenom-People-hosted career site — an ATS none of
this project's other connectors support — including a real "Seasonal HR
Intern" posting. This is the thing to reach for first when adding a new
company that isn't on Greenhouse/Lever/Workday, before writing a new
vendor-specific connector: check `<company>/robots.txt` for a sitemap,
find the one listing individual job URLs (not just category/landing
pages — UPS publishes both, only one is useful), and see if a job page
has `<script type="application/ld+json">...JobPosting...`.

**Tier 2 — vendor-specific per-company connectors.** Still useful, not
replaced: highest precision for a company you're specifically targeting,
and a fallback for the (rare) company that neither an aggregator indexes
nor exposes a job sitemap. This is
`scraper/connectors/{greenhouse,lever,workday}.py` plus
`docs/adding-a-source.md`. Reach for Tier 1.5 before writing a new one of
these — it only pays off when a company happens to already run
Greenhouse, Lever, or Workday.

## Sourcing vs. filtering are two different questions, on purpose

Earlier versions of this project answered "what postings exist" and "which
ones do I want" in the same place — a source was scraped AND filtered to
ops/logistics/supply-chain in one step (`is_relevant()` in what was then
`filters.py`, with a per-source `domain_native` escape hatch). That
collapsed as soon as the project needed to serve more than one person's
interest: a marketing-track reader forking this repo would have inherited
a scraper that had already thrown their postings away before they ever
saw them.

The fix was to split the two concerns cleanly:

- `scraper/scrape.py` + `sources.yaml` answer ONLY "is this an internship
  posting" (title contains "intern"/"internship"/"co-op") and write
  everything that passes to `data/all_postings.json` — the complete,
  domain-unfiltered raw feed, every industry The Muse's `level=Internship`
  query covers plus every Tier 1.5/Tier 2 source.
- `filters.yaml` + `scraper/user_filter.py` + `scraper/build_feed.py`
  answer "which of those do I actually want," applied AFTER scraping,
  against the same shared raw store. This fork's own `filters.yaml` is
  ops/logistics/supply-chain by default, but it's config, not code — copy
  it, point `build_feed.py` at your copy, get your own feed with zero
  re-scraping and zero code changes.

## A concrete lesson from building this

An aggregator's own category taxonomy is not the same thing as any
particular reader's relevance filter, and trusting it blindly is a
mistake — proven live, back when sourcing and filtering were still one
step: The Muse's "Business Operations" category (with `level=Internship`)
returned 160 postings, but a full 146 of them were hotel-administration
internships at Selina and social-media account-management internships at
TikTok, not anything industrial/supply-chain/logistics. A keyword check
on top of the aggregator's results — now living in `filters.yaml` rather
than baked into the Muse connector — cut that down with zero loss of the
real hits. See `filters.yaml`'s own comment on why bare "operations" was
dropped as a keyword (matched that same noise, added no genuine hits
beyond more specific terms already there).
