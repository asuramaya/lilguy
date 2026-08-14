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
companies it's told to try (`CANDIDATE_SEED` in the file, or any row
someone else adds to `discovery_candidates`) — it discovers WHICH ats
platform a named company uses automatically, it doesn't invent company
names out of nothing. For each due candidate: try Greenhouse, then
Lever, then a small bounded Workday tenant/site guess matrix, then a
jsonld sitemap check, in that order (cheapest/most reliable first). A
hit gets a REAL trial fetch through the matching connector, then has to
pass `service/verify.py`'s gate before it's trusted at all.

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

Everything lives in one Postgres container + `pgdata` volume — bring it
down with `docker compose down` (add `-v` to also drop the data) and back
up with `docker compose up -d` on any host with Docker, which is the
whole point of building this as a self-hosted container stack rather
than a specific cloud platform's managed primitives.

## What this explicitly does NOT do (yet)

- **Horizontal scaling of the scheduler.** One `scheduler.py` process is
  the only thing bounding concurrent fetches (via `SELECT ... FOR UPDATE
  SKIP LOCKED` claims, which ARE safe against concurrent callers, but two
  full scheduler replicas would each independently think they have
  `MAX_WORKERS` free slots). This project's source count doesn't need
  that yet.
- **Broad automatic company discovery.** `CANDIDATE_SEED` still needs a
  human (or a future job) to name companies to try — discovery
  automates the "which ATS" question per named company, not "which
  companies exist."
- **Oracle Recruiting Cloud / Eightfold / Phenom-style discovery.**
  Those platforms use opaque per-company hosts that plain HTTP probing
  can't guess — every one found in this project so far (Honeywell,
  Eaton, ITW) needed a live browser session reading real network
  requests. `discovery.py`'s probes are Greenhouse/Lever/Workday-guess/
  jsonld only, honestly, not a claim to cover every ATS shape.
