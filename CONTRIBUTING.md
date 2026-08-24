# Contributing

This project is two things: a scraper that finds internship postings
across many companies (`sources.yaml` → `data/all_postings.json`), and a
set of filters that turn that raw data into a view someone actually wants
(`filters.yaml` / `presets/*.yaml` → a rendered feed). Most contributions
fall into one of those two buckets, and they have different bars.

## Adding a source (a new company/aggregator)

Read `docs/sourcing-model.md` first for the three-tier shape (aggregator →
generic JSON-LD harvester → vendor-specific connector) and
`docs/adding-a-source.md` for the concrete steps. The short version:

1. Run `python scraper/discover.py --company "..." <domain>` first: it
   automates the Greenhouse/Lever/jsonld checks and tells you plainly when
   it can't find anything (usually meaning a browser session is actually
   needed, not that something's broken). Try Tier 1.5 (`ats: jsonld`,
   which works on any career site with a job sitemap + schema.org/JobPosting
   markup) before writing new connector code. Most companies you'd think
   to add turn out to work here.
2. **Verify it before opening a PR**: run `python scraper/scrape.py` and
   confirm your new source shows a real `fetched -> internship-shaped`
   count, not a silent zero. A PR whose source config was never actually
   run against the live site isn't reviewable. "I added Caterpillar" and
   "I confirmed Caterpillar returns real postings" are different claims,
   and this project has a documented history of the two disagreeing
   (Honeywell was assumed to run Workday and actually runs Oracle
   Recruiting Cloud, found by checking, not assuming).
3. Include what you found in the PR description: which tier, which URL
   pattern, and the actual command output showing it working.

## Adding or improving a filter preset

`presets/` are ready-made `filters.yaml`-shaped files for a specific
interest (software engineering, marketing, finance...). To add one:

1. Copy the closest existing preset as a starting point.
2. **Sanity-check it against the real raw store before proposing it**:
   `python scraper/build_feed.py --filters presets/yours.yaml --out /tmp/test.md`
   and actually read a sample of what came out. A keyword list that
   sounds right and one that's been checked against real postings are not
   the same thing; this project has caught real false-positive keywords
   this way more than once (bare "operations" matched hotel-admin and
   TikTok account-management postings; "logistics" alone matches "event
   logistics" in unrelated marketing copy). Zero results or clearly wrong
   results usually means a keyword is either too narrow or too generic.
3. Prefer specific multi-word phrases over single generic words. The
   project's own experience is that generic single words are where
   precision breaks down.

## Code changes to the scraper itself

- Keep the sourcing/filtering separation intact: `scraper/scrape.py` and
  everything in `sources.yaml` should only ever answer "does an
  internship-shaped posting exist here," never "is this the right kind of
  internship." That judgment belongs in a filter file. If you're adding
  code that makes a scraping decision based on domain/category, it
  probably belongs in `user_filter.py` or a preset instead.
- A connector that fails should fail LOUDLY (raise with a clear message)
  rather than silently return an empty list. See any existing connector's
  error handling for the pattern. A silent zero and a genuine "no postings
  right now" are indistinguishable to a reader, and this project has hit
  that exact bug class before (see the decisions in this repo's own git
  history / the reasoning in `docs/`).
- Run `python -m py_compile scraper/*.py scraper/connectors/*.py` and the
  full `python scraper/scrape.py` before opening a PR, since this project
  doesn't have CI wired up yet (a legitimate contribution if you want to
  add it), so a manual run is the only check that currently exists.

## What this project won't take

See `autofill/README.md` for a deliberate scope boundary: no full
auto-submit for applications, on purpose, not an oversight. A PR that adds
automatic form submission (as opposed to autofill-and-stop) will be
declined for the reasons documented there, not because the code is bad.
