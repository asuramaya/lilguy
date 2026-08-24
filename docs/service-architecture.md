# The live service (`service/`)

`scraper/scrape.py` is a batch job: it runs once, fetches every source
sequentially, writes `data/all_postings.json`, and exits. That's simple
and worked fine for a small source list, but it doesn't scale as a "live
feed." This project's own history proves it: once a couple of
sources each took several minutes (Eaton, ITW, hundreds of paced
requests apiece), one full sequential run stretched past 15-20 minutes,
and every new source added from there makes it linearly longer.

`service/` is a second, DB-backed way to run the exact same sourcing
logic: continuously, concurrently, and with the source list itself
growing on its own. It does NOT replace `scraper/scrape.py` or the git-
committed `data/all_postings.json` / `FEED.md` workflow; both can keep
running side by side. `service/` is for anyone who wants to self-host a
genuinely live version instead of (or alongside) the daily GitHub Actions
batch job.

## What stays the same

Every connector in `scraper/connectors/*.py` (Greenhouse, Lever,
Workday, Oracle Recruiting Cloud, the generic jsonld harvester, Muse)
is reused completely unchanged. Each one already takes a plain
`entry: dict` and returns `list[Posting]`; `service/scheduler.py` just
gets that dict from a Postgres row's `config` JSONB column instead of a
`sources.yaml` list item, and writes results into `postings` rows instead
of appending to a JSON file. The hard-won parts of this project, the
per-request pacing that avoids WAF triggers, the retry-on-transient-
failure logic, the "a source failing to fetch is not the same fact as it
reporting zero postings" guarantee, all still apply exactly as before.

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
source has its OWN re-scrape interval (an aggregator every hour, a slow
industrial every 6-12h) rather than one global daily run. This is the
actual fix for the sustainability problem: sources run concurrently and
on independently-sized cadences, so the pipeline's wall-clock time
doesn't keep growing linearly as sources are added the way the
sequential batch script's did.

**discovery.py** is the "self-updating list" half. It can only probe
companies it's told to try. It discovers WHICH ats platform a named
company uses automatically, it doesn't invent company names out of
nothing. As of this writing, "told to try" means `service/
candidate_sources.py`'s `fetch_sec_edgar_company_names()`: SEC EDGAR's
own public `company_tickers.json`, 10,391 real company names, free, no
API key, no signup, confirmed live. That's a real jump from this
project's original 9-name hand-typed placeholder list, though it's
worth being honest about its own limit: EDGAR only covers publicly
traded US companies, so large private employers (a lot of trucking/
freight carriers, for instance) aren't in it at all. See `candidate_
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
is really that company's internship board." This project hit that
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

A pass doesn't go straight to `active`. It goes to `probation` with a
short (1h) re-scrape interval. `scheduler.py`'s NORMAL next poll cycle
re-fetches it; only a SECOND independent success (checked there, via
`source["last_scraped_at"] is not None` meaning a prior cycle already
happened) promotes it to `active`. A failure on that confirmation
attempt rejects it instead, with evidence preserved in
`discovery_candidates`, not silently dropped. No separate "wait 24h and
recheck" job exists for this; probation sources just ride the same
scheduler everything else does, with a shorter interval. Two
independent successful fetches, spaced apart, replaces "one clean
response, trust it forever." That's a materially higher bar, achieved with no
human or agent review step, per the actual ask that produced this
design.

**Self-healing runs the other direction too.** Any `active` source that
racks up `FAILURE_DISABLE_THRESHOLD` (5) consecutive failures auto-
demotes to `disabled` (its existing postings are left untouched, not
deleted or closed: a source failing to fetch still isn't the same fact
as it reporting nothing). `discovery.py`'s `recheck_disabled_sources()`
periodically gives disabled sources one more trial fetch through the
SAME gate a brand-new candidate faces; a pass sends it back to
`probation` (re-earning its confirmation, not trusted immediately) since
one working fetch after a string of failures could itself be a fluke.

## Cross-source deduplication

Once discovery promotes a company to its own direct connector, that
company's postings can arrive via TWO paths at once: an aggregator
(Muse) that already indexed it, and the new direct source. Nothing in
the per-source upsert catches this (it only ever looks at one source's
fresh postings at a time). `service/dedup.py` closes the gap with a
normalized `dedup_key` (company + title + location, legal-suffix- and
punctuation-insensitive) and a stateless, idempotent sweep: rank every
group of same-key postings by source precedence (a company's own direct
ATS connector outranks an aggregator; ties broken by whichever this
project saw first), keep the top-ranked one `open`, mark the rest
`duplicate`. Stateless matters here: the sweep recomputes the winner
from scratch every run, so if the canonical posting later closes, the
next sweep naturally promotes the best remaining duplicate back to
`open` with no special-case code for that transition. `scheduler.py`
runs it once per cycle, after that cycle's upserts.

## Company, source and ATS are three different things

The word "source" was doing three jobs at once here: a board we poll, the
platform it runs on, and where a given posting came from. The
conflation looked harmless because most sources happen to be 1:1 with a
company. They are not, and the exception is the majority of the feed.

The connectors make the split explicit. A direct connector reads the
company from OUR config, so it is constant for that source:

```python
company=entry.get("company", token)          # greenhouse, lever, workday, oracle, jsonld
```

An aggregator reads it from each individual job:

```python
company=(job.get("company") or {}).get("name", "")    # muse
```

So one Muse source row carries 2798 of 4833 open postings across 87
different employers, and its `sources.company` is a label, literally
`"The Muse (aggregator — every real category, queried separately)"`,
not a company at all.

The model that follows:

| | what it is | spans |
|---|---|---|
| **company** | an employer; what a reader wants | MANY sources, its own board *and* an aggregator |
| **source** | one board we poll; health, cadence, failures | one ATS |
| **ATS** | the platform; explains why fields differ | many sources |

`postings.company_key` is the join key for the company view, normalized by
`dedup.compute_company_key`, the SAME rule as the company component of
`dedup_key`, shared deliberately so "same company" cannot mean two things
in two places. Measured before adopting: across 102 distinct raw company
strings it merged exactly two pairs, both correct (`Eaton`/`Eaton
Corporation`, `Samsara`/`Samsara Inc.`), with no false merges. It
united four employers reachable through two sources each, which would
otherwise have rendered as two half-empty pages.

**This distinction has caused two real bugs, both this project's own.**
`source_sync.py`'s sweep synced `postings.company` from `sources.company`
for every posting, which is right for a direct board and flattened all
2798 Muse postings to the aggregator's label; it is now scoped to
`dedup.DIRECT_SOURCE_ATS`. And an absent description stored as `''`
rather than `NULL` marked every new Workday posting "already attempted",
hiding it from its own backfill. Both were the same assumption: that a
field which is source-derived for one connector family is source-derived
for all of them. Check which family you are in before syncing anything
from `sources` onto `postings`.

The UI reflects it: company names open a company page (union across
sources), Sources rows open an operational source page, and a source page
offers a direct jump to its company ONLY when it carries a single
employer: `api.source()` reports `is_aggregator` from
`count(DISTINCT company_key)` rather than from a hardcoded list of
aggregator names.

## Job descriptions

`postings.description_snippet` is whitespace-collapsed and capped at 600
characters because it feeds keyword MATCHING (`user_filter.passes`).
That is correct for matching and useless for reading, so it was the only
description this project kept and a posting page had nothing to show.

The surprise on inspection: for ~83% of the corpus the full text was
ALREADY in the list response being fetched and then truncated:
Greenhouse's `content`, Muse's `contents`, Lever's `descriptionPlain`,
JSON-LD's `description`, Oracle's three composable fields. Storing it
cost zero extra HTTP requests. `postings.description` holds it, converted
by `to_display_text` rather than `strip_html`: block tags become real
line breaks and list items become bullets, so a description renders with
`white-space: pre-wrap` and no sanitizer. It emits TEXT, never markup.
These strings come from third-party boards, and storing HTML we later
inject would mean owning an XSS surface forever.

**Workday is the sole exception** and needs a second request per posting;
its list endpoint carries no description at all (`bulletFields[0]` is a
requisition-ID stub). `service/workday_descriptions.py` fetches from the
CXS detail endpoint, and the cost is bounded by NEW postings rather than
total ones because the column is three-state:

| value | meaning |
|---|---|
| `NULL` | never attempted, eligible |
| `''` | attempted; the provider genuinely has none, or it is gone |
| text | got it |

A transient failure (timeout, 5xx) deliberately leaves `NULL` so it
retries; a definitive 404/410 writes `''` so a posting that will never
resolve is not re-requested every cycle forever. Confusing those two is
how a backfill becomes permanent load on someone else's servers. The
scheduler's upsert COALESCEs rather than overwrites for the same reason:
Workday's connector sends empty every cycle, and a plain overwrite would
erase the fetched text and re-fetch it forever.

## Locations are free text

There are 1,785 distinct location strings across ~4,600 open postings.
They come from whatever each employer typed, and they are not
normalized: "Remote", "Remote - United States", "Flexible / Remote",
"United States - Remote" and "2 Locations" all occur.

That is fine for DISPLAY and for SEARCH (the full-text vector includes
location, so searching "Chicago" works). It is not enough for a location
FILTER: a dropdown of 1,785 options is not a control, and matching
"New York" against that spread needs real normalization first. Nobody
should promise location filtering without doing that work.

The one place this already mattered is the Google Maps link. A pin on a
whole country is a worse answer than no link, so `placeParts()` in
index.html strips working-arrangement words ("Remote", "Hybrid",
"Flexible") and country-level terms ("USA", "United States", "EMEA")
and only links if a real place survives. "Remote - New York" pins New
York; "Remote - USA" gets no link. Covered by
`tests/frontend/test_maps_url.js`, which extracts the functions out of
index.html rather than copying them, so the test cannot drift from what
ships.

## Exposing this publicly

This deployment binds `127.0.0.1:8000` and is reached over Tailscale, so
it is not on the public internet. If you fork this and put it on one,
two things are your responsibility rather than the app's, and both are
deliberate omissions rather than oversights.

**There is no rate limiting.** Not in the app, not planned. What the app
does instead is remove the reason one was needed: `/feed` used to re-read
the entire open corpus from Postgres on every request, so a flood of
requests was a flood of database reads. That read is now an in-process
snapshot with a short TTL (`CORPUS_TTL_SECONDS`), which means a burst of
*distinct* queries costs one database read per TTL rather than one per
request. A response cache would not have achieved this: keyed on the
query string, `?q=<random>` walks straight past it.

What that does not cover is bandwidth and connection exhaustion, which
genuinely belong to a reverse proxy. Put nginx or Caddy in front and set
limits there.

**The API runs a single uvicorn worker.** The corpus cache is
process-local, so adding `--workers` does not break correctness (each
worker keeps its own snapshot), but the workers may be up to one TTL
apart from each other. Know that before you add them.

Security headers (CSP, `nosniff`, referrer policy, frame-ancestors) ARE
set on every response, in `SECURITY_HEADERS`. The CSP is strict because
the frontend is a single static file with all CSS and JS inline and no
external requests, so forbidding everything else costs nothing.

## Deploying

```
scripts/run_tests.sh              # whole suite against a throwaway Postgres
scripts/deploy.sh api scheduler   # test, push, rebuild, then VERIFY
```

`deploy.sh` runs everything that can refuse before anything that mutates:
tests, clean-tree check, push, rebuild. That ordering is what turns a
network fault into a no-op: a push that fails leaves nothing
half-applied, which is how the ~100-minute connectivity loss on
2026-08-17 cost nothing.

Then `scripts/verify_deploy.sh` confirms the deploy actually WORKS, which
is a different claim from "started":

- snapshot each service's `RestartCount`, wait out a settle window, **fail
  if it moved**
- confirm each container is in state `running` (an unresolvable name
  resolves to `missing`, so a typo'd service fails rather than being
  silently skipped)
- ask the api's `/health` whether it serves rather than merely runs

**The delta is the load-bearing part, not the status.** `docker compose up
-d` returns when containers start, and a crashlooping container reads
`running` every time you look at it. Measured on this host against a
container crashing every 3s: status was `running` at all five samples
while the restart count climbed 0→4. A `docker ps` gate passes five times
out of five on a service that has never once stayed up. Reading the count
once tells you nothing either; only its change over a window does.

What this does NOT cover: a 20s window catches a fast crashloop, not a
slow leak or a death at minute three. That case is covered by a different
layer: `container-watch` on the host (a systemd timer, every 5 minutes,
watching every container whose own restart policy declares it should stay
up). The two are deliberately not redundant: `container-watch` answers
"is it staying up", `verify_deploy.sh` answers "is it serving", and a
service that is up and returning errors passes the former cleanly.

`verify_deploy.sh` is a separate script rather than inline in `deploy.sh`
specifically so its FAILURE path can be run on demand: pointed at a
deliberately crashlooping container, it has been observed tripping (exit
1), passing on healthy services (exit 0), and rejecting a typo'd name. A
gate whose failure has never executed is a belief, not a check.

## Reconciling with the batch pipeline

`scraper/scrape.py`'s git-committed `data/all_postings.json`/`FEED.md`
and the live service's Postgres `postings` table are two independent
systems that will drift apart with no automatic sync. Deliberately not
solved by picking a winner: `service/export_to_batch_store.py` proves
the two are reconcilable BY CONSTRUCTION (it reads live Postgres and
writes the exact same JSON shape `store.py` already produces, verified
by literally running the exported file through `build_feed.py`'s own
`build_and_write()` in `tests/service/test_export_to_batch_store.py`)
without deciding WHEN it should run. That's a deployment-cadence
question (a cron container? manual? on every promotion?), and this
project isn't deciding deployment yet. See the standing decision on
hyper-docker + a `cupid` handoff over Osiris mail, once that phase
starts.

## Tuning the verification gate with real data

`verify.py`'s thresholds were reasoned from specific failures found by
hand this session, not measured against real discovery results: there
weren't any yet when they were set. `service/analyze_discovery_evidence.py`
reads back the evidence every `verify_trial_fetch()` call already writes
into `discovery_candidates.evidence` and buckets it by outcome (rejection
reasons tallied, `name_similarity`/`intern_count` distributions split
promoted-vs-rejected, and a join against `sources` showing how many
first-gate "promoted" candidates actually went on to confirm to
`active`). Doesn't change any threshold itself, but turns "does 0.5 feel
about right" into something answerable once the discovery loop has
actually run for a while.

## Categorizing discovery-promoted sources

`discovery.py`'s probes (Greenhouse/Lever/Workday-guess/jsonld) confirm a
company has a real, matching career site, but they have no way to know what
industry that company is actually in, so every auto-promoted source lands
with `category = 'Uncategorized'` (see the literal in each `_try_*` probe
function). This is deliberate, not a bug: guessing a category from a
company name would be exactly the kind of unverified claim this project's
own standard rules out.

Categorizing is a periodic manual pass, not a one-time fix. Find the
backlog with:

```sql
SELECT company FROM sources WHERE category = 'Uncategorized' ORDER BY company;
```

Research each company's real business (this session used WebSearch to
confirm, not guessed from the name), then update BOTH `category` and
`config->>'category'`: the connector reads category from `config` at
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
combination of two. "Freight Brokerage" and "Freight Forwarding" are
different businesses (C.H. Robinson vs. Flexport) that both used to sit
under one generic "Logistics & Transportation" bucket; splitting by real
business model is more useful than it looks with only ~40 sources, because
each source *is* a meaningful fraction of the list. Compound names stay
only where they're a real, standard sector name (`Aerospace & Defense`,
`Freight & Trucking`) rather than two unrelated things stitched together
for convenience (the old `CPG / Consumer Brands` was the latter, renamed
to plain `Consumer Packaged Goods`).

**At more than a handful of companies, do this with a saved workflow
instead of by hand.** `.claude/workflows/categorize-sources.js` is a
reusable Claude Code workflow (not a one-off script written into a
session's scratch space). It fans out one agent per ~20-token batch,
each doing real web research per company (never guessing from the token
alone), and returns `{token, company_name, category, note}` for every
company, including an honest `"Unidentified"` for ones no agent could
confirm rather than a fabricated guess. Apply the result with
`scripts/apply_categorization.py`, which is careful about the same
things a hand-written SQL pass should be: per-row transactions (one
collision can't roll back 400 good updates), and a check against
existing `(company, ats)` pairs before any rename. Confirmed live this
matters (see git history: cleaning up a real batch surfaced 6 genuine
duplicate sources scraping the same company twice under different
casing, and 2 look-alike collisions that turned out to be legitimately
separate boards, not duplicates: the collision check is what caught the
difference instead of silently merging or crashing).

```
# 1. Pull the current backlog
psql ... -c "SELECT company FROM sources WHERE category='Uncategorized' ORDER BY company;"

# 2. Run the workflow (from a Claude Code session in this repo)
Workflow({name: "categorize-sources", args: ["acme", "beta", ...]})
# save the returned array as results.json

# 3. Apply it
DATABASE_URL=... python3 scripts/apply_categorization.py results.json
```

This backlog isn't a one-time debt: every discovery promotion lands
`Uncategorized` by design (see above), so it grows continuously as
discovery finds new companies. There's no persistent process able to run
this on its own cadence (a Claude Code cron job is session-bound and
would silently stop firing; see the "Staying informed" section above
for why the same constraint ruled out a similar always-on notifier), so
this stays something a human triggers periodically, just with the actual
research work no longer needing to be redesigned from scratch each time.

## Running it

```
docker compose up -d --build
curl localhost:8000/health
curl localhost:8000/feed                                    # this fork's default filter
curl "localhost:8000/feed?preset=software-engineering"      # any preset in presets/
curl localhost:8000/sources                                  # per-source status, cadence, failure counts
curl localhost:8000/candidates                               # discovery evidence, promoted/rejected/no_match
curl "localhost:8000/feed?preset=all&q=logistics&max_age_days=30"  # server-side search + freshness
curl localhost:8000/feed.atom                                # subscribe in any feed reader
curl "localhost:8000/feed?preset=all&limit=200&offset=200"   # paging; `total` is independent of the page
curl localhost:8000/company/rocketlab                        # one employer, across every source
curl localhost:8000/posting/<id>                             # one posting + duplicates + sibling roles
curl localhost:8000/source/1                                 # one board: health, cadence, employers carried
curl localhost:8000/ats/workday                              # one platform and its quirks
scripts/run_tests.sh                                          # whole suite against a scratch Postgres
scripts/deploy.sh api                                         # test, push, rebuild, verify (refuses on red)
scripts/resolve_source_names.py                               # ask boards their real name (dry run; --apply)
docker compose logs -f scheduler                              # watch it fetch in real time
```

Open `http://localhost:8000/` in a browser for a read-only UI over those
same endpoints (`service/api.py` mounts `service/static/`): a Feed tab
(preset switcher, plus server-side title/company search, category and
freshness filters over open postings, paged with load-more), a Sources
tab (per-source status/failure table), a Discovery tab (candidate review
status with the reason each rejection was recorded), and a Duplicates tab
(what the dedup sweep collapsed). Clicking through opens entity pages
inside the app rather than bouncing out to the ATS. See "Company, source
and ATS are three different things" above.

Search and filtering run on the SERVER: they used to run in the browser
over whatever page had loaded, which meant a search silently covered only
the newest slice of the corpus and reported the result as complete.
Paging is offset-based, and the feed query's ORDER BY ends in `id` for
that reason: without a unique final key the sort is not total, Postgres
may order tied rows differently per query, and paging then repeats and
skips rows. Measured: 88 open postings share one identical
`(posted_at_ts, first_seen)` pair, and three pages of 200 returned 592
distinct rows out of 600 before the tiebreaker was added.

Deliberately plain
HTML/CSS/vanilla JS, no build step and no npm dependency. It's fetched
by the browser at runtime from whatever origin served the page, so
nothing needs rebuilding when the data changes. This is the entire
frontend this project has; it's read-only by design (no create/edit/
delete anywhere), matching the project's own "sourcing is automated,
applying is a human decision" stance from `autofill/README.md`.

Everything lives in one Postgres container + `pgdata` volume. Bring it
down with `docker compose down` (add `-v` to also drop the data) and back
up with `docker compose up -d` on any host with Docker, which is the
whole point of building this as a self-hosted container stack rather
than a specific cloud platform's managed primitives.

## Backups

The `pgdata` volume was NOT backed up before this was added. Only the git
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
which the host's existing TrueNAS pull already collects; no separate
off-host transport needed. It refuses to keep a suspiciously small dump
(a truncated file is worse than no file: it passes an "it exists" check
while being useless).

Crontab on the deploy host (`crontab -e` as the `agent` user):

```
0 3 * * *  /srv/internships/scripts/backup_postgres.sh  >> /var/log/internships-backup.log 2>&1
0 4 * * 0  /srv/internships/scripts/restore_test_backup.sh >> /var/log/internships-backup.log 2>&1
```

**"The dump wrote" and "the dump restores" are different claims.** This
project already found a real bug (a promoted source's `next_check_at`
never being set, corrupting `discovery_candidates` audit state) purely by
restoring a real backup and querying it, not by trusting that `pg_dump`
exiting 0 meant the data was sound. Don't skip the restore-test script in
practice just because the nightly dump "worked."

## Auditing liveness itself

`service/liveness.py`'s sweep closes anything it recognizes as gone,
every scheduler cycle, forever -- but it has no way to notice when its
OWN recognition logic has gone stale. Three real, distinct bugs shipped
this way in one session: Workday's per-job CXS endpoint 403ing on
demonstrably open postings (treated as gone), Greenhouse never 404ing a
dead job at all (a 200 redirect to `?error=true` instead), and
SmartRecruiters' rendered job page staying a full 200 long after the
posting closed (its own API's `active` field was the only real signal).
Each was found by a human/agent sitting down and sampling the corpus by
hand -- not something that repeats itself on its own.

`scripts/audit_liveness.py` is the mechanical version of that same
process: it re-checks a live sample of open postings (stratified per
ATS, oldest-first -- same bias as `liveness.py`'s own claim query, same
reason) against `check_posting_status`, the SAME function the sweep
itself uses (imported, not re-implemented -- two copies of this logic
drifting apart is exactly how the Workday bug shipped undetected as
long as it did). It closes whatever it confirms dead, same as the
sweep, and it writes one `liveness_audit` event per run reporting the
per-ATS discrepancy rate. A rate above 5% for some platform is the
signal that platform needs a bespoke check the way Workday, Greenhouse
and SmartRecruiters got -- printed as an `ALERT` line and a non-zero
exit code, not just a number nobody looks at.

```
scripts/audit_liveness.py                    # sample + report, closes confirmed-dead
scripts/audit_liveness.py --no-close          # report only, touch nothing
scripts/audit_liveness.py --sample-size 300   # bigger sample per ATS
```

Crontab on the deploy host:

```
30 3 * * *  cd /srv/internships && docker compose run --rm api python scripts/audit_liveness.py >> /var/log/internships-liveness-audit.log 2>&1
```

`--input data/all_postings.json` remains as an offline/read-only
fallback for testing against the git-committed batch export instead of
the live DB -- reporting only, since a static snapshot has no live row
to close.

## Staying informed

Considered and rejected: an email/webhook notifier that pushes on a new
source promotion, a source going `disabled`, or a backup restore-test
result, so nobody has to check by hand. Not built, for a concrete reason
rather than lack of effort: there's no persistent process positioned to
send it. The `scheduler`/`discovery` containers have no SMTP or webhook
credentials (and adding them means new secrets to manage for a marginal
win), and a Claude Code cron job is session-bound and would silently stop
firing when that session ends, which is worse than no notifier at all:
it would look like monitoring while actually monitoring nothing.

What's built instead: an `events` table (`service/schema.sql`) that
`scheduler.py`, `discovery.py`, and `scripts/restore_test_backup.sh` write
to directly, a `/events` API endpoint, and a badge in the frontend header
that shows "N new" the moment the page loads (unseen state tracked in
`localStorage`, so it resets per browser). This is "proactive" in the one
sense actually achievable without new infrastructure (surfaced
immediately on load instead of requiring a dig through the Sources/
Discovery tabs), not a true push notification. If this project ever adds
its own SMTP credentials for another reason, revisit wiring `events` rows
into an actual outbound notifier at that point.

**Update: postings themselves now have a real subscription path.** The
reasoning above still holds for *push*, but it conflated "we can't push"
with "you have to come look", and those aren't the same thing.
`/feed.atom` serves any `/feed` query as an Atom document, so a reader
polls it on its own schedule and new postings arrive without anyone
opening the page. That needs no persistent process on our side, no
credentials and no account (the three constraints that killed the push
options), and the subscriber controls both frequency and unsubscribing.

```
curl "localhost:8000/feed.atom"                            # this fork's default filter
curl "localhost:8000/feed.atom?preset=all&max_age_days=7"  # everything posted in the last week
curl "localhost:8000/feed.atom?q=logistics&limit=20"
```

Entries are dated by the **employer's** posting date, not by when this
service discovered them, so a posting found today but posted in March
doesn't arrive looking new (see "Posting dates" below). The feed's own
`<updated>` is its newest entry rather than `now()`, so an unchanged feed
doesn't register as changed on every poll. The `events` table remains the
right mechanism for *operational* news (a source promoted or disabled),
a different audience from "a new internship exists".

## Posting dates

`postings.posted_at` holds whatever the provider sent, and providers do
not agree: ISO-8601 from Greenhouse/Muse/JSON-LD/Oracle,
bare epoch **milliseconds** from Lever, and English prose from Workday
("Posted Today", "Posted 2 Days Ago", "Posted 30+ Days Ago"). Because it
was `TEXT`, nothing could sort or filter on it, so the feed sorted and
displayed `first_seen` (*our* discovery time) under a column headed
"Posted". With a database younger than the postings it holds, that made
every row read as hours old, including Lever postings genuinely from 2021.

`service/posted_at.py` normalizes all three families into `posted_at_ts`
(timestamptz) plus `posted_at_approx`. The raw text column is kept as
provenance so a future parser fix can be replayed against the original
value rather than a lossy conversion: that's what
`scripts/backfill_posted_at.py` is for; it's safe to re-run.

Two things worth knowing:

- **`posted_at_approx` is not decoration.** Workday's "30+ Days Ago" is a
  *saturating upper bound*, not a measurement. Re-resolving it on each
  scrape would push the bound forward forever, so a six-month-old posting
  would report as 30 days old indefinitely: exactly the staleness this
  column exists to expose. The upsert therefore keeps the **earliest**
  estimate for approximate values and the newest for exact ones.
- **How stale the corpus actually is.** A snapshot, so treat the exact
  numbers as drifting, but the proportions are stable and they are the
  point. Of 4833 open postings: 870 were posted in the last week, 1809
  are older than 90 days, and **1208 are older than a year**, the oldest
  dating to 2016-02-24. Those are boards that never took the listing
  down. Re-run it with:

  ```sql
  SELECT count(*) FILTER (WHERE posted_at_ts < now() - interval '365 days'),
         count(*) FROM postings WHERE status = 'open';
  ```

  `max_age_days` is the filter for this, and it judges on the employer's
  date, so it now means what it says.

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
  entirely: it doesn't guess at company names, it directly enumerates
  real, currently-crawled Greenhouse boards and Workday tenants, so
  `discovery.py` seeds those as already-resolved candidates and skips
  guessing outright. Confirmed live: ~4,000 real Greenhouse tokens and
  ~1,100 real Workday tenant/host/site triples per pull, including exact
  values (`wd_host: "wd3"`, `site: "Sierra_Space_External_Career_Site"`)
  the guess matrix would never have found on its own. Deliberately does
  NOT cover Lever: `jobs.lever.co/robots.txt` explicitly disallows
  Common Crawl's bot, so there's almost nothing indexed there to find.
  Still not exhaustive: a public S&P 1500/Russell index constituent
  list is a real next step, not yet wired in (both are effectively
  paywalled as machine-readable data outside a handful of already-
  EDGAR-covered large caps; the freely-scrapable Wikipedia "constituents
  of the S&P 500" table is almost entirely public companies already in
  the EDGAR set, so it wasn't worth adding for the private-company gap
  specifically).
- **Oracle Recruiting Cloud / Eightfold / Phenom-style discovery.**
  Those platforms use opaque per-company hosts that plain HTTP probing
  can't guess: every one found in this project so far (Honeywell,
  Eaton, ITW) needed a live browser session reading real network
  requests. `discovery.py`'s probes are Greenhouse/Lever/Workday-guess/
  jsonld only, honestly, not a claim to cover every ATS shape.
