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
GE Vernova, and Walmart internships with zero per-company setup. Adzuna
(`scraper/connectors/adzuna.py`) is wired up the same way as a second
aggregator, gated behind a free API key the repo doesn't ship (see the
commented-out entry in `sources.yaml`).

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

## A concrete lesson from building Tier 1

An aggregator's own category taxonomy is not the same thing as this
project's relevance filter, and trusting it blindly is a mistake — proven
live: The Muse's "Business Operations" category (with `level=Internship`)
returned 160 postings, but a full 146 of them were hotel-administration
internships at Selina and social-media account-management internships at
TikTok, not anything industrial/supply-chain/logistics. Running this
project's own `filters.py` keyword check on top of the aggregator's
results (rather than trusting `domain_native` the way a Tier 2 freight
broker earns it) cut that to 14 genuinely relevant postings with zero
loss of the real hits — see `sources.yaml`'s comment on the Muse entry and
`filters.py`'s comment on why bare "operations" was removed as a keyword.
