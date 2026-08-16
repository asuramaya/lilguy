# The live service (`service/`)

`scraper/scrape.py` is a batch job: it runs once, fetches every source
sequentially, writes `data/all_postings.json`, and exits. That's simple
and worked fine for a small source list, but it doesn't scale as a "live
feed" — proven live in this project's own history: once a couple of
sources each took several minutes (Eaton, ITW — hundreds of paced
requests apiece), one full sequential run stretched past 15-20 minutes,
and every new source added from there makes it linearly longer.

`service/` is a second, DB-backed way to run the exact same sourcing
logic — continuously, concurrently, and with the source list itself
growing on its own. It does NOT replace `scraper/scrape.py` or the git-
committed `data/all_postings.json` / `FEED.md` workflow; both can keep
running side by side. `service/` is for anyone who wants to self-host a
genuinely live version instead of (or alongside) the daily GitHub Actions
batch job.

## What stays the same

Every connector in `scraper/connectors/*.py` — Greenhouse, Lever,
Workday, Oracle Recruiting Cloud, the generic jsonld harvester, Muse —
is reused completely unchanged. Each one already takes a plain
`entry: dict` and returns `list[Posting]`; `service/scheduler.py` just
gets that dict from a Postgres row's `config` JSONB column instead of a
`sources.yaml` list item, and writes results into `postings` rows instead
of appending to a JSON file. The hard-won parts of this project — the
per-request pacing that avoids WAF triggers, the retry-on-transient-
failure logic, the "a source failing to fetch is not the same fact as it
reporting zero postings" guarantee — all still apply exactly as before.

## The pieces

```
docker-compose.yml
├── postgres      -- the one stateful piece
├── migrate       -- one-shot: creates the schema, imports sources.yaml
├── scheduler.py  -- runs forever: concurrent, per-source-cadence fetching
├── discovery.py  -- runs forever: candidate probing + self-healing
└── api.py        -- FastAPI: /feed, /sources, /candidates over HTTP
```

**scheduler.py** polls `sources` for rows whose `next_scrape_at` has
passed, dispatches up to `MAX_WORKERS` (default 6) into a thread pool
concurrently, and reconciles each fetch's results into `postings`
(open/closed by presence, same semantics as the old `store.rebuild()`,
just scoped to one source at a time instead of one global batch). Each
source has its OWN re-scrape interval — an aggregator every hour, a slow
industrial every 6-12h — rather than one global daily run. This is the
actual fix for the sustainability problem: sources run concurrently and
on independently-sized cadences, so the pipeline's wall-clock time
doesn't keep growing linearly as sources are added the way the
sequential batch script's did.

**discovery.py** is the "self-updating list" half. It can only probe
companies it's told to try — it discovers WHICH ats platform a named
company uses automatically, it doesn't invent company names out of
nothing. As of this writing, "told to try" means `service/
candidate_sources.py`'s `fetch_sec_edgar_company_names()` — SEC EDGAR's
own public `company_tickers.json`, 10,391 real company names, free, no
API key, no signup, confirmed live. That's a real jump from this
project's original 9-name hand-typed placeholder list, though it's
worth being honest about its own limit: EDGAR only covers publicly
traded US companies, so large private employers (a lot of trucking/
freight carriers, for instance) aren't in it at all — see `candidate_
sources.py`'s own module docstring for what else was researched and
rejected (SIC-code industry filtering, genuinely too fragile on SEC's
legacy search endpoint to depend on) and why full breadth was chosen
over an industry-prioritized subset. For each due candidate: try
Greenhouse, then Lever, then a small bounded Workday tenant/site guess
matrix, then a jsonld sitemap check, in that order (cheapest/most
reliable first). A hit gets a REAL trial fetch through the matching
connector, then has to pass `service/verify.py`'s gate before it's
trusted at all.

## Why auto-promotion needed a gate, not just "did the request succeed"

A probe returning "HTTP 200, valid JSON" is a different fact from "this
is really that company's internship board" — this project hit that
distinction three separate times by hand in one session: a wrong
Workday tenant/site guess can 404/422 cleanly (easy), but Parker
Hannifin's site returned a real-looking Cloudflare challenge page,
Union Pacific's SuccessFactors board had real job data as microdata
instead of the JSON-LD this project's connector reads, and (in the
design conversation that led to this file) a Workday tenant/site guess
can resolve to a DIFFERENT real company's board entirely if the tenant
string happens to collide. None of those are hypothetical edge cases;
all three came up naturally while building this project's source list.

`verify.py`'s gate checks the ACTUAL trial-fetch data, not just that a
request succeeded:
- non-zero total postings
- at least one posting whose title matches this project's own
  internship-shaped regex (the same one `scraper/filters.py` uses)
- postings aren't all identical (guards against a placeholder/error page
  that happens to parse as valid data)
- the fetched postings' company field fuzzy-matches the candidate name
  being searched for (guards against a tenant/site collision resolving
  to an unrelated company)

A pass doesn't go straight to `active` — it goes to `probation` with a
short (1h) re-scrape interval. `scheduler.py`'s NORMAL next poll cycle
re-fetches it; only a SECOND independent success (checked there, via
`source["last_scraped_at"] is not None` meaning a prior cycle already
happened) promotes it to `active`. A failure on that confirmation
attempt rejects it instead — evidence preserved in
`discovery_candidates`, not silently dropped. No separate "wait 24h and
recheck" job exists for this; probation sources just ride the same
scheduler everything else does, with a shorter interval. Two
independent successful fetches, spaced apart, replaces "one clean
response, trust it forever" — a materially higher bar, achieved with no
human or agent review step, per the actual ask that produced this
design.

**Self-healing runs the other direction too.** Any `active` source that
racks up `FAILURE_DISABLE_THRESHOLD` (5) consecutive failures auto-
demotes to `disabled` (its existing postings are left untouched, not
deleted or closed — a source failing to fetch still isn't the same fact
as it reporting nothing). `discovery.py`'s `recheck_disabled_sources()`
periodically gives disabled sources one more trial fetch through the
SAME gate a brand-new candidate faces; a pass sends it back to
`probation` (re-earning its confirmation, not trusted immediately) since
one working fetch after a string of failures could itself be a fluke.

## Cross-source deduplication

Once discovery promotes a company to its own direct connector, that
company's postings can arrive via TWO paths at once — an aggregator
(Muse) that already indexed it, and the new direct source. Nothing in
the per-source upsert catches this (it only ever looks at one source's
fresh postings at a time). `service/dedup.py` closes the gap with a
normalized `dedup_key` (company + title + location, legal-suffix- and
punctuation-insensitive) and a stateless, idempotent sweep: rank every
group of same-key postings by source precedence (a company's own direct
ATS connector outranks an aggregator; ties broken by whichever this
project saw first), keep the top-ranked one `open`, mark the rest
`duplicate`. Stateless matters here — the sweep recomputes the winner
from scratch every run, so if the canonical posting later closes, the
next sweep naturally promotes the best remaining duplicate back to
`open` with no special-case code for that transition. `scheduler.py`
runs it once per cycle, after that cycle's upserts.

## Reconciling with the batch pipeline

`scraper/scrape.py`'s git-committed `data/all_postings.json`/`FEED.md`
and the live service's Postgres `postings` table are two independent
systems that will drift apart with no automatic sync. Deliberately not
solved by picking a winner — `service/export_to_batch_store.py` proves
the two are reconcilable BY CONSTRUCTION (it reads live Postgres and
writes the exact same JSON shape `store.py` already produces, verified
by literally running the exported file through `build_feed.py`'s own
`build_and_write()` in `tests/service/test_export_to_batch_store.py`)
without deciding WHEN it should run. That's a deployment-cadence
question (a cron container? manual? on every promotion?), and this
project isn't deciding deployment yet — see the standing decision on
hyper-docker + a `cupid` handoff over Osiris mail, once that phase
starts.

## Tuning the verification gate with real data

`verify.py`'s thresholds were reasoned from specific failures found by
hand this session, not measured against real discovery results — there
weren't any yet when they were set. `service/analyze_discovery_evidence.py`
reads back the evidence every `verify_trial_fetch()` call already writes
into `discovery_candidates.evidence` and buckets it by outcome (rejection
reasons tallied, `name_similarity`/`intern_count` distributions split
promoted-vs-rejected, and a join against `sources` showing how many
first-gate "promoted" candidates actually went on to confirm to
`active`). Doesn't change any threshold itself — turns "does 0.5 feel
about right" into something answerable once the discovery loop has
actually run for a while.

## Categorizing discovery-promoted sources

`discovery.py`'s probes (Greenhouse/Lever/Workday-guess/jsonld) confirm a
company has a real, matching career site — they have no way to know what
industry that company is actually in, so every auto-promoted source lands
with `category = 'Uncategorized'` (see the literal in each `_try_*` probe
function). This is deliberate, not a bug: guessing a category from a
company name would be exactly the kind of unverified claim this project's
own standard rules out.

Categorizing is a periodic manual pass, not a one-time fix — find the
backlog with:

```sql
SELECT company FROM sources WHERE category = 'Uncategorized' ORDER BY company;
```

Research each company's real business (this session used WebSearch to
confirm, not guessed from the name), then update BOTH `category` and
`config->>'category'` — the connector reads category from `config` at
scrape time (see `scraper/connectors/base.py`), so only updating the
`category` column changes what `/sources` reports without changing what
new postings actually get tagged as:

```sql
UPDATE sources
SET category = 'Defense Technology',
    config = jsonb_set(config, '{category}', '"Defense Technology"')
WHERE company = 'Shield AI';
```

Categories should be one specific thing, not a slash- or ampersand-joined
combination of two — "Freight Brokerage" and "Freight Forwarding" are
different businesses (C.H. Robinson vs. Flexport) that both used to sit
under one generic "Logistics & Transportation" bucket; splitting by real
business model is more useful than it looks with only ~40 sources, because
each source *is* a meaningful fraction of the list. Compound names stay
only where they're a real, standard sector name (`Aerospace & Defense`,
`Freight & Trucking`) rather than two unrelated things stitched together
for convenience (the old `CPG / Consumer Brands` was the latter — renamed
to plain `Consumer Packaged Goods`).

## Running it

```
docker compose up -d --build
curl localhost:8000/health
curl localhost:8000/feed                                    # this fork's default filter
curl "localhost:8000/feed?preset=software-engineering"      # any preset in presets/
curl localhost:8000/sources                                  # per-source status, cadence, failure counts
curl localhost:8000/candidates                               # discovery evidence, promoted/rejected/no_match
docker compose logs -f scheduler                              # watch it fetch in real time
```

Open `http://localhost:8000/` in a browser for a minimal read-only UI over
those same three endpoints (`service/api.py` mounts `service/static/`) —
a Feed tab (preset switcher + client-side title/company search over open
postings), a Sources tab (per-source status/failure table), and a
Discovery tab (candidate review status, filterable). Deliberately plain
HTML/CSS/vanilla JS, no build step and no npm dependency — it's fetched
by the browser at runtime from whatever origin served the page, so
nothing needs rebuilding when the data changes. This is the entire
frontend this project has; it's read-only by design (no create/edit/
delete anywhere), matching the project's own "sourcing is automated,
applying is a human decision" stance from `autofill/README.md`.

Everything lives in one Postgres container + `pgdata` volume — bring it
down with `docker compose down` (add `-v` to also drop the data) and back
up with `docker compose up -d` on any host with Docker, which is the
whole point of building this as a self-hosted container stack rather
than a specific cloud platform's managed primitives.

## Backups

The `pgdata` volume was NOT backed up before this was added — only the git
repo was (via an hourly bundle), which covers code and `sources.yaml` but
none of the live `postings` / `discovery_candidates` state. Two scripts fix
that:

```
scripts/backup_postgres.sh        # cron this nightly on the deploy host
scripts/restore_test_backup.sh    # run weekly (or before trusting a backup
                                   # for a real recovery) -- restores the
                                   # latest dump into a throwaway container
                                   # and diffs row counts against live
```

`backup_postgres.sh` writes `pg_dump` output to `/var/backups/internships/`,
which the host's existing TrueNAS pull already collects — no separate
off-host transport needed. It refuses to keep a suspiciously small dump
(a truncated file is worse than no file: it passes an "it exists" check
while being useless).

Crontab on the deploy host (`crontab -e` as the `agent` user):

```
0 3 * * *  /srv/internships/scripts/backup_postgres.sh  >> /var/log/internships-backup.log 2>&1
0 4 * * 0  /srv/internships/scripts/restore_test_backup.sh >> /var/log/internships-backup.log 2>&1
```

**"The dump wrote" and "the dump restores" are different claims** — this
project already found a real bug (a promoted source's `next_check_at`
never being set, corrupting `discovery_candidates` audit state) purely by
restoring a real backup and querying it, not by trusting that `pg_dump`
exiting 0 meant the data was sound. Don't skip the restore-test script in
practice just because the nightly dump "worked."

## Staying informed

Considered and rejected: an email/webhook notifier that pushes on a new
source promotion, a source going `disabled`, or a backup restore-test
result, so nobody has to check by hand. Not built, for a concrete reason
rather than lack of effort — there's no persistent process positioned to
send it. The `scheduler`/`discovery` containers have no SMTP or webhook
credentials (and adding them means new secrets to manage for a marginal
win), and a Claude Code cron job is session-bound and would silently stop
firing when that session ends, which is worse than no notifier at all —
it would look like monitoring while actually monitoring nothing.

What's built instead: an `events` table (`service/schema.sql`) that
`scheduler.py`, `discovery.py`, and `scripts/restore_test_backup.sh` write
to directly, a `/events` API endpoint, and a badge in the frontend header
that shows "N new" the moment the page loads (unseen state tracked in
`localStorage`, so it resets per browser). This is "proactive" in the one
sense actually achievable without new infrastructure — surfaced
immediately on load instead of requiring a dig through the Sources/
Discovery tabs — not a true push notification. If this project ever adds
its own SMTP credentials for another reason, revisit wiring `events` rows
into an actual outbound notifier at that point.

## What this explicitly does NOT do (yet)

- **Horizontal scaling of the scheduler.** One `scheduler.py` process is
  the only thing bounding concurrent fetches (via `SELECT ... FOR UPDATE
  SKIP LOCKED` claims, which ARE safe against concurrent callers, but two
  full scheduler replicas would each independently think they have
  `MAX_WORKERS` free slots). This project's source count doesn't need
  that yet.
- **Candidate breadth beyond SEC EDGAR + Wikipedia + Common Crawl.** SEC
  EDGAR (10,391 public companies) and Wikipedia's industry category
  listings (~1,300 more, reaching real PRIVATE carriers like AAA Cooper
  Transportation that EDGAR structurally can't) both feed plain company
  NAMES into the existing guess-probe pipeline. Common Crawl's free CDX
  index (`candidate_sources.py`'s `fetch_commoncrawl_greenhouse_tokens`/
  `fetch_commoncrawl_workday_tenants`) is a different kind of source
  entirely — it doesn't guess at company names, it directly enumerates
  real, currently-crawled Greenhouse boards and Workday tenants, so
  `discovery.py` seeds those as already-resolved candidates and skips
  guessing outright. Confirmed live: ~4,000 real Greenhouse tokens and
  ~1,100 real Workday tenant/host/site triples per pull, including exact
  values (`wd_host: "wd3"`, `site: "Sierra_Space_External_Career_Site"`)
  the guess matrix would never have found on its own. Deliberately does
  NOT cover Lever — `jobs.lever.co/robots.txt` explicitly disallows
  Common Crawl's bot, so there's almost nothing indexed there to find.
  Still not exhaustive — a public S&P 1500/Russell index constituent
  list is a real next step, not yet wired in (both are effectively
  paywalled as machine-readable data outside a handful of already-
  EDGAR-covered large caps; the freely-scrapable Wikipedia "constituents
  of the S&P 500" table is almost entirely public companies already in
  the EDGAR set, so it wasn't worth adding for the private-company gap
  specifically).
- **Oracle Recruiting Cloud / Eightfold / Phenom-style discovery.**
  Those platforms use opaque per-company hosts that plain HTTP probing
  can't guess — every one found in this project so far (Honeywell,
  Eaton, ITW) needed a live browser session reading real network
  requests. `discovery.py`'s probes are Greenhouse/Lever/Workday-guess/
  jsonld only, honestly, not a claim to cover every ATS shape.
