"""Fully automated candidate probing + promotion -- no human or agent
review step (see docs/service-architecture.md for why a review QUEUE was
rejected in favor of this).

Two loops live here:

  run_discovery_cycle() -- for each due candidate in discovery_candidates
  (or a brand-new name from CANDIDATE_SEED not yet tracked at all), try
  each ATS probe in turn (cheapest/most reliable first), run a REAL trial
  fetch through the matching connector when a probe hits, and pass it
  through verify.py's gate. A pass inserts into `sources` with
  status='probation' -- NOT active yet. scheduler.py's normal poll loop
  then re-fetches it on its own schedule; only a SECOND independent
  success (confirmed there, not here) promotes it to active. A fail is
  recorded with evidence and pushed out to a long recheck interval (ATS
  platforms do change over time, so "no match today" isn't permanent,
  but retrying daily would just be noise).

  recheck_disabled_sources() -- the symmetric self-healing half: a
  `sources` row that scheduler.py auto-disabled after repeated failures
  (a site redesign, a moved tenant, a dead board) gets one trial fetch
  here too. If it now looks alive again by the SAME verify.py gate, it
  goes back to probation (not straight to active) -- rejoining the same
  two-strike confirmation path a brand-new candidate would, since "it
  used to work" doesn't mean the fetch that "fixed" it isn't itself
  another fluke.

Discovery can only probe companies it's been TOLD to try -- it finds
which ATS a named company uses automatically, it does not invent company
names from nothing. That distinction matters: the automation this
project asked for is "no human needed to APPROVE a hit," not "no human
ever names a company to check." As of this file, "told to try" means
SEC EDGAR's own public company-tickers list (10,391 real company names)
plus Wikipedia's industry category listings, both free, no API key, no
signup (see candidate_sources.py) -- rather than a hand-typed list
someone has to keep extending by hand. Both of those still name a
COMPANY and let the existing guess-probe matrix figure out the ATS.

Common Crawl's free CDX index is a different shape of "told to try"
entirely -- instead of naming a company and guessing its ATS, it directly
enumerates real, currently-crawled Greenhouse boards and Workday tenants
(see candidate_sources.py's fetch_commoncrawl_* functions), so those get
seeded as ALREADY-RESOLVED candidates with no guessing left to do (see
_seed_commoncrawl_candidates_if_due() and _process_candidate()'s
pre-resolved-config check below). Confirmed live to find real Workday
host/site values (e.g. wd_host="wd3", a real production Workday pod
never in WORKDAY_HOST_GUESSES) that blind guessing structurally never
would.
"""
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import psycopg2.extras
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent))

from connectors import CONNECTORS  # noqa: E402

from candidate_sources import (  # noqa: E402
    fetch_commoncrawl_greenhouse_tokens,
    fetch_commoncrawl_workday_tenants,
    fetch_sec_edgar_company_names,
    fetch_wikipedia_category_companies,
)
from db import cursor  # noqa: E402
from verify import verify_trial_fetch  # noqa: E402

# Confirmed live: a full 8,480-candidate queue at the original TIMEOUT=12,
# fully sequential, averaged 5.8s/candidate -- a ~13.7 HOUR full pass.
# These probes are lightweight existence checks against real APIs, not
# real scraping: a token/tenant that actually exists responds fast, and
# a guess that doesn't exist should fail fast too (DNS/connection
# refused) rather than need 12s to time out. 5s is still generous for
# that. See the concurrency changes below (per-candidate Workday
# parallelization + across-candidate dispatch) for the rest of the fix
# -- timeout alone only bounds the tail, it doesn't fix the structural
# cost of running everything sequentially.
TIMEOUT = 5
UA = {"User-Agent": "Mozilla/5.0 (internship-feed-bot; auto-discovery)"}
TRIAL_MAX_PAGES = 40  # small trial fetch -- enough to judge, not a full scrape
NO_MATCH_RECHECK_DAYS = 90
REJECTED_RECHECK_DAYS = 90

# The real candidate feed: SEC EDGAR's public-company ticker list
# (10,391 names) PLUS Wikipedia's category listings for trucking/
# logistics/manufacturing/freight (a few hundred more, reaching real
# PRIVATE companies EDGAR structurally can't) -- see candidate_
# sources.py for what each covers, what was tried and rejected for each
# (SIC-code industry filtering for EDGAR, guessed category titles for
# Wikipedia), and their own honestly-stated coverage gaps. Deliberately
# NOT fetched at import time -- `import discovery` must stay a pure,
# network-free operation (tests import this module on every run; a
# module-level network call would make every test run slow and flaky
# against either source's uptime, not just the tests that actually
# exercise seeding). _load_candidate_seed() is called lazily, inside
# _seed_unchecked_candidates(), only when a real seeding pass runs.
# A small hand-picked fallback covers BOTH sources being unreachable
# (e.g. offline dev) so discovery still has SOMETHING to work with
# rather than silently seeding nothing.
FALLBACK_CANDIDATE_SEED = ["Ford", "Toyota", "Lockheed Martin", "Cummins", "Caterpillar",
                            "Deere", "Nucor", "Emerson Electric", "Rockwell Automation"]


def _load_candidate_seed() -> list[str]:
    # Two independent sources, each fault-tolerant on its own -- SEC
    # EDGAR reaches public companies, Wikipedia's category listings reach
    # real PRIVATE companies EDGAR structurally can't (see candidate_
    # sources.py's own docstrings for what each does and doesn't cover).
    # Falls back to the hardcoded list only if BOTH fail, so one source
    # having a bad day doesn't zero out the whole seed the way it would
    # if this were a single try/except around a single call.
    names: list[str] = []
    try:
        names.extend(fetch_sec_edgar_company_names())
    except Exception as exc:  # noqa: BLE001 - seeding must not crash the whole loop
        print(f"[discovery] SEC EDGAR candidate fetch failed ({exc})", flush=True)
    try:
        names.extend(fetch_wikipedia_category_companies())
    except Exception as exc:  # noqa: BLE001
        print(f"[discovery] Wikipedia candidate fetch failed ({exc})", flush=True)
    return names or FALLBACK_CANDIDATE_SEED


# None means "not overridden" -- _seed_unchecked_candidates() calls
# _load_candidate_seed() fresh each time. Tests set this directly via
# monkeypatch to inject a fixed list without touching the network (see
# tests/service/test_discovery.py) -- same pattern as before, just no
# longer evaluated at import time.
CANDIDATE_SEED = None

# Common Crawl's own index only refreshes roughly monthly, and a full
# pull (Greenhouse's two domains + Workday's several pages) confirmed
# live at ~1-2 minutes total -- re-running that every 5-minute discovery
# cycle the way _load_candidate_seed() does for SEC EDGAR/Wikipedia would
# stall the loop for no benefit (nothing upstream has changed). Gated to
# once a day instead. Module-level, resets on container restart -- worst
# case is one extra re-seed after a restart, not a correctness issue
# since every insert is ON CONFLICT DO NOTHING anyway.
COMMONCRAWL_RESEED_INTERVAL = timedelta(days=1)
_last_commoncrawl_seed_at: datetime | None = None


def _existing_source_keys() -> set[tuple[str, str]]:
    # (lowercased company, ats) pairs already live in `sources` --
    # confirmed live as a REAL collision, not a hypothetical: Common
    # Crawl's Workday pull found tenant "3m" (lowercase, from the real
    # URL), which is a DIFFERENT string than the existing manual source
    # "3M" -- Postgres's UNIQUE (company, ats) constraint on `sources` is
    # case-sensitive, so without this check that candidate would promote
    # into a SECOND, redundant source scraping the exact same board.
    # discovery_candidates' own UNIQUE(company) + ON CONFLICT DO NOTHING
    # already prevents exact-string duplicates within that table; this
    # closes the separate case-insensitive gap against `sources` itself.
    with cursor() as cur:
        cur.execute("SELECT company, ats FROM sources")
        return {(r["company"].lower(), r["ats"]) for r in cur.fetchall()}


def _seed_commoncrawl_candidates_if_due() -> None:
    global _last_commoncrawl_seed_at
    now = datetime.now(timezone.utc)
    if _last_commoncrawl_seed_at is not None and now - _last_commoncrawl_seed_at < COMMONCRAWL_RESEED_INTERVAL:
        return
    _last_commoncrawl_seed_at = now
    existing = _existing_source_keys()

    try:
        tokens = fetch_commoncrawl_greenhouse_tokens()
    except Exception as exc:  # noqa: BLE001 - seeding must not crash the whole loop
        print(f"[discovery] Common Crawl Greenhouse fetch failed ({exc})", flush=True)
        tokens = []
    tokens = [t for t in tokens if (t.lower(), "greenhouse") not in existing]
    if tokens:
        # Plain candidate names, same shape as the SEC EDGAR/Wikipedia
        # seed -- _probe_greenhouse() already slugifies before querying,
        # a no-op on an already-slug-shaped token, so these flow through
        # the EXACT existing probe/verify/promote pipeline with zero
        # further changes.
        with cursor() as cur:
            psycopg2.extras.execute_values(
                cur, "INSERT INTO discovery_candidates (company) VALUES %s ON CONFLICT (company) DO NOTHING",
                [(t,) for t in tokens],
            )
        print(f"[discovery] seeded {len(tokens)} Greenhouse candidate(s) from Common Crawl", flush=True)

    try:
        workday_triples = fetch_commoncrawl_workday_tenants()
    except Exception as exc:  # noqa: BLE001
        print(f"[discovery] Common Crawl Workday fetch failed ({exc})", flush=True)
        workday_triples = []
    workday_triples = [t for t in workday_triples if (t["tenant"].lower(), "workday") not in existing]
    if workday_triples:
        # Pre-resolved (ats, config) rows, NOT plain names -- Common Crawl
        # already gave us a real, confirmed-live (tenant, host, site)
        # triple, so there's nothing left to guess. _process_candidate
        # checks for a pre-populated row["ats"]/row["config"] and skips
        # straight to a trial fetch through this exact config instead of
        # running the guess-probe matrix.
        rows = []
        for t in workday_triples:
            config = {"company": t["tenant"], "ats": "workday", "tenant": t["tenant"], "wd_host": t["wd_host"],
                      "site": t["site"], "category": "Uncategorized", "max_pages": 5}
            rows.append((t["tenant"], "workday", psycopg2.extras.Json(config)))
        with cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO discovery_candidates (company, ats, config) VALUES %s ON CONFLICT (company) DO NOTHING",
                rows,
            )
        print(f"[discovery] seeded {len(workday_triples)} Workday candidate(s) from Common Crawl", flush=True)

# Small, bounded guess matrices -- NOT exhaustive. Confirmed live this
# session that blind Workday guessing has a low hit rate (Ford/Toyota/
# Lockheed all 422'd on the obvious combinations) -- that's an accepted,
# documented limitation, not a bug to chase with a bigger matrix. A
# company this misses still gets caught by a human doing what this
# project's docs/adding-a-source.md describes (a live browser session).
WORKDAY_HOST_GUESSES = ["wd1", "wd5"]
WORKDAY_SITE_GUESSES = ["External", "Careers", "Search"]

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
LEVER_API = "https://api.lever.co/v0/postings/{token}?mode=json"
JOB_URL_HINTS = re.compile(r"/(job|jobs|position|req|career)s?[/_-]", re.IGNORECASE)


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _guess_domains(company: str) -> list[str]:
    # Two guesses, not one -- confirmed live (task #23, this session) that
    # `careers.{slug}.com` is a real, common pattern this probe was
    # entirely missing: PepsiCo, Honeywell, and General Mills (three of
    # this project's OWN existing sources) all redirect from exactly this
    # subdomain. The original single-guess version only ever tried the
    # bare root domain. (Considered guessing Oracle Recruiting Cloud
    # hosts too, for the same "grow the probe matrix" reason -- confirmed
    # NOT viable: oracle_recruiting.py's own docstring documents that
    # host as an opaque per-company hash Oracle assigns at provisioning
    # [Honeywell's is "ibqbjb"], with no relationship to the company name
    # to guess from. A guessing probe there would just be firing at
    # random strings hoping to hit an unrelated company's cloud pod, not
    # a real probe -- left alone rather than forced.)
    slug = _slugify(company)
    return [f"{slug}.com", f"careers.{slug}.com"]


def _probe_greenhouse(company: str):
    token = _slugify(company)
    try:
        # content=true costs nothing extra (same request either way) and
        # gets back each job's real company_name -- confirmed live this
        # matters for Common-Crawl-sourced candidates specifically (task
        # #candidate-sources), whose seeded "company" is just the raw
        # URL token (e.g. "10xgenomics"), not a real display name. Every
        # posting from this source would otherwise show that raw slug as
        # its company forever (Posting.company comes straight from this
        # config's "company" field -- see scraper/connectors/base.py).
        r = requests.get(GREENHOUSE_API.format(token=token), params={"content": "true"}, timeout=TIMEOUT, headers=UA)
        jobs = r.json().get("jobs") if r.status_code == 200 else None
        if jobs:
            display_name = jobs[0].get("company_name") or company
            return {"ats": "greenhouse", "config": {"company": display_name, "ats": "greenhouse", "token": token,
                                                      "category": "Uncategorized", "max_pages": TRIAL_MAX_PAGES}}
    except Exception:  # noqa: BLE001 - a bad guess is a miss, never a crash (see _probe_jsonld's own note)
        pass
    return None


def _probe_lever(company: str):
    token = _slugify(company)
    try:
        r = requests.get(LEVER_API.format(token=token), timeout=TIMEOUT, headers=UA)
        if r.status_code == 200 and isinstance(r.json(), list) and r.json():
            return {"ats": "lever", "config": {"company": company, "ats": "lever", "token": token,
                                                 "category": "Uncategorized", "max_pages": TRIAL_MAX_PAGES}}
    except Exception:  # noqa: BLE001
        pass
    return None


def _try_workday_guess(company: str, tenant: str, host: str, site: str):
    url = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    try:
        r = requests.post(url, json={"limit": 1, "offset": 0, "searchText": ""}, timeout=TIMEOUT,
                           headers={"Content-Type": "application/json"})
        if r.status_code == 200 and "jobPostings" in r.json():
            return {"ats": "workday", "config": {"company": company, "ats": "workday", "tenant": tenant,
                                                    "wd_host": host, "site": site,
                                                    "category": "Uncategorized", "max_pages": 5}}
    except Exception:  # noqa: BLE001 - `tenant` is a hostname label built from a company name we
        pass           # don't control (e.g. an overlong slug raises urllib3.LocationParseError,
    return None         # not requests.RequestException -- confirmed live, see _probe_jsonld's note)


def _probe_workday(company: str):
    # The 6 host/site combinations are independent guesses about the SAME
    # company -- order doesn't matter, only "did any of them hit." Firing
    # them concurrently instead of sequentially was the single biggest
    # per-candidate latency cost in this module: confirmed live, a full
    # sequential pass at TIMEOUT=12 averaged 5.8s/candidate, and this is
    # the only probe that always runs multiple requests even when nothing
    # matches (Greenhouse/Lever are one request each; jsonld usually
    # dies at the first robots.txt check for a non-career site).
    #
    # as_completed, not pool.map -- map() yields results back in
    # SUBMISSION order, so a slow guess submitted first would still block
    # returning an early hit found by a guess submitted later, even
    # though both ran concurrently. as_completed returns whichever
    # future finishes FIRST, so a real hit short-circuits the moment it
    # lands instead of waiting on whatever happens to be earlier in the
    # host/site list.
    #
    # Deliberately NOT `with ThreadPoolExecutor() as pool:` -- the
    # context manager's __exit__ calls shutdown(wait=True), which blocks
    # until EVERY submitted future finishes, even ones we've already
    # stopped caring about after an early hit. That would silently
    # cancel out most of the point of racing these guesses in the first
    # place. shutdown(wait=False) lets this function return the moment a
    # hit is found; the handful of still-running background requests
    # finish on their own without this call waiting on them.
    tenant = _slugify(company)
    guesses = [(host, site) for host in WORKDAY_HOST_GUESSES for site in WORKDAY_SITE_GUESSES]
    pool = ThreadPoolExecutor(max_workers=len(guesses))
    try:
        futures = [pool.submit(_try_workday_guess, company, tenant, host, site) for host, site in guesses]
        for future in as_completed(futures):
            result = future.result()
            if result:
                return result
        return None
    finally:
        pool.shutdown(wait=False)


def _fetch_sitemap_locs(url: str):
    try:
        r = requests.get(url, timeout=TIMEOUT, headers=UA)
        if r.status_code == 200:
            return re.findall(r"<loc>([^<]+)</loc>", r.text)
    except Exception:  # noqa: BLE001 - a malformed/unreachable sitemap URL is a miss, not a crash
        pass
    return []


def _probe_jsonld(company: str, domain: str):
    # A guessed domain can be malformed in ways requests.RequestException
    # doesn't cover -- confirmed live, this crashed the whole discovery
    # loop into a restart crash-loop (task #23 fallout): a garbage
    # candidate name ("List of biotech and pharmaceutical companies in
    # the New York metropolitan area" -- a Wikipedia LIST article, not a
    # company, that slipped through candidate_sources.py's filtering)
    # slugified into a 70+ character domain label, and urllib3 raises
    # LocationParseError for that at DNS-resolution time, one layer below
    # what requests.RequestException catches. A probe guessing at
    # something is allowed to guess wrong; it is never allowed to take
    # the whole process down over it -- catch broadly here, same
    # philosophy as _process_candidate's own trial-fetch exception guard
    # below.
    try:
        r = requests.get(f"https://{domain}/robots.txt", timeout=TIMEOUT, headers=UA)
    except Exception:  # noqa: BLE001
        return None
    if r.status_code != 200:
        return None
    sitemap_urls = re.findall(r"^\s*sitemap:\s*(\S+)", r.text, re.IGNORECASE | re.MULTILINE)
    for sm in sitemap_urls[:5]:
        locs = _fetch_sitemap_locs(sm)
        sub_sitemaps = [loc for loc in locs if loc.split("?", 1)[0].lower().endswith(".xml")]
        if sub_sitemaps and len(sub_sitemaps) == len(locs):
            locs = [loc for sub in sub_sitemaps[:5] for loc in _fetch_sitemap_locs(sub)]
        job_like = [loc for loc in locs if JOB_URL_HINTS.search(urlparse(loc).path)]
        if job_like:
            pattern = urlparse(job_like[0]).path.rsplit("/", 1)[0] + "/"
            return {"ats": "jsonld", "config": {"company": company, "ats": "jsonld", "sitemap_url": sm,
                                                  "url_pattern": pattern, "category": "Uncategorized",
                                                  "max_pages": TRIAL_MAX_PAGES}}
    return None


PROBES = [_probe_greenhouse, _probe_lever, _probe_workday]


def probe_candidate(company: str) -> dict | None:
    """Try each ATS probe in order, cheapest first. jsonld needs a domain
    guess and is the least reliable of the four, so it goes last -- and
    tries each domain guess in turn (root + careers subdomain) rather
    than giving up after the first miss."""
    for probe in PROBES:
        hit = probe(company)
        if hit:
            return hit
    for domain in _guess_domains(company):
        hit = _probe_jsonld(company, domain)
        if hit:
            return hit
    return None


def _seed_unchecked_candidates():
    # Re-fetched every cycle rather than cached -- SEC EDGAR's own list
    # grows as companies IPO, so this naturally picks up new entrants
    # over time. Cheap to repeat: ON CONFLICT DO NOTHING makes re-seeding
    # an already-known company a no-op, and the whole batch goes in as
    # ONE query (psycopg2.extras.execute_values), not one round trip per
    # company -- with a real ~10K-name feed instead of the original
    # 9-name hardcoded list, a per-row INSERT loop here would have meant
    # ~10K network round trips every single discovery cycle.
    seed = CANDIDATE_SEED if CANDIDATE_SEED is not None else _load_candidate_seed()
    if not seed:
        return
    with cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO discovery_candidates (company) VALUES %s ON CONFLICT (company) DO NOTHING",
            [(c,) for c in seed],
        )


# Candidates are independent -- each probes a DIFFERENT company's own
# domain, so there's no shared-site politeness concern the way
# scheduler.py's scraping concurrency has to stay conservative about
# (MAX_WORKERS=6 there, bounding how many DIFFERENT companies get
# scraped at once mostly to bound local resource use, not to protect
# any one site). These probes are cheap existence checks, not real
# scraping -- confirmed live, the whole discovery process uses under
# 30MB RAM and ~5% CPU even before this fix. 24 is a deliberately
# generous worker count given that headroom: at the measured 5.8s/
# candidate sequential baseline, 24-way concurrency alone gets an
# 8,480-candidate queue down to roughly (8480 * 5.8s) / 24 ≈ 34 minutes
# -- combined with the per-candidate Workday parallelization and the
# tighter TIMEOUT above, comfortably under an hour with real margin.
DISCOVERY_CANDIDATE_WORKERS = 24


def _process_candidate(row: dict) -> dict:
    """One candidate's full probe -> trial fetch -> verify -> DB-write
    pipeline. Extracted from run_discovery_cycle so it can be dispatched
    into a thread pool -- each call opens its own DB connection (see
    db.py's connect(), a fresh psycopg2.connect() per call, not a shared
    pool), so concurrent calls from different threads are safe without
    any locking here beyond the row-level FOR UPDATE SKIP LOCKED claim
    already taken before dispatch.
    """
    company = row["company"]
    now = datetime.now(timezone.utc)
    # Defense in depth on top of each probe's own broad except clauses
    # (see _probe_jsonld's note) -- an uncaught exception here isn't just
    # a bad candidate, it kills this worker thread inside pool.map(),
    # which propagates out of run_discovery_cycle() and crashes
    # run_forever()'s while loop entirely. Docker's restart policy then
    # brings the process back up, but the row's SELECT ... FOR UPDATE
    # lock was already released before dispatch (see db.py), so the SAME
    # candidate gets claimed again on the very next cycle -- a real
    # crash loop, confirmed live in production (task #23 fallout,
    # 2026-08-16), not a hypothetical.
    if row.get("ats") and row.get("config"):
        # Pre-resolved by a seeding source that already confirmed the
        # real (ats, config) -- e.g. Common Crawl's Workday tenant/host/
        # site triples (see candidate_sources.py). Nothing left to guess,
        # so skip the probe entirely and go straight to a real trial
        # fetch through this exact config.
        hit = {"ats": row["ats"], "config": row["config"]}
    else:
        try:
            hit = probe_candidate(company)
        except Exception as exc:  # noqa: BLE001
            with cursor() as cur:
                cur.execute(
                    "UPDATE discovery_candidates SET review_status = 'no_match', checked_at = %s, "
                    "next_check_at = %s WHERE id = %s",
                    (now, now + timedelta(days=NO_MATCH_RECHECK_DAYS), row["id"]),
                )
            return {"company": company, "outcome": "no_match", "reason": f"probe raised: {exc}"}

    if hit is None:
        with cursor() as cur:
            cur.execute(
                "UPDATE discovery_candidates SET review_status = 'no_match', checked_at = %s, "
                "next_check_at = %s WHERE id = %s",
                (now, now + timedelta(days=NO_MATCH_RECHECK_DAYS), row["id"]),
            )
        return {"company": company, "outcome": "no_match"}

    connector_cls = CONNECTORS[hit["ats"]]
    try:
        trial_postings = connector_cls().fetch(hit["config"])
    except Exception as exc:  # noqa: BLE001
        verdict = {"passed": False, "reason": f"trial fetch raised: {exc}", "evidence": {}}
    else:
        verdict = verify_trial_fetch(company, trial_postings)

    with cursor() as cur:
        if verdict["passed"]:
            cur.execute(
                """
                INSERT INTO sources (company, ats, category, config, status, added_by, scrape_interval_seconds)
                VALUES (%s, %s, 'Uncategorized', %s, 'probation', 'discovery', 3600)
                ON CONFLICT (company, ats) DO NOTHING
                """,
                (company, hit["ats"], psycopg2.extras.Json(hit["config"])),
            )
            cur.execute(
                "UPDATE discovery_candidates SET ats = %s, config = %s, review_status = 'promoted', "
                "evidence = %s, checked_at = %s WHERE id = %s",
                (hit["ats"], psycopg2.extras.Json(hit["config"]), psycopg2.extras.Json(verdict["evidence"]),
                 now, row["id"]),
            )
            cur.execute(
                "INSERT INTO events (kind, company, detail) VALUES ('promoted', %s, %s)",
                (company, f"auto-promoted to probation via {hit['ats']}"),
            )
            return {"company": company, "outcome": "promoted_to_probation", "ats": hit["ats"]}
        else:
            cur.execute(
                "UPDATE discovery_candidates SET ats = %s, config = %s, review_status = 'rejected', "
                "evidence = %s, checked_at = %s, next_check_at = %s WHERE id = %s",
                (hit["ats"], psycopg2.extras.Json(hit["config"]),
                 psycopg2.extras.Json({**verdict["evidence"], "reason": verdict["reason"]}),
                 now, now + timedelta(days=REJECTED_RECHECK_DAYS), row["id"]),
            )
            return {"company": company, "outcome": "rejected", "reason": verdict["reason"]}


def run_discovery_cycle(limit: int = 5, max_workers: int = DISCOVERY_CANDIDATE_WORKERS) -> list[dict]:
    """Processes up to `limit` due candidates, up to `max_workers` at a
    time. Returns a result summary per candidate for logging."""
    _seed_unchecked_candidates()
    _seed_commoncrawl_candidates_if_due()

    with cursor() as cur:
        # 'promoted' is deliberately EXCLUDED, not just "not due yet" --
        # a promoted candidate's fate from here on lives in `sources.
        # status` (probation -> active or rejected, handled by
        # scheduler.py), not in this table. Confirmed live as a real
        # bug, not a hypothetical: the promotion UPDATE never set
        # next_check_at, which defaults to now() at insert time --
        # `next_check_at <= now()` was therefore ALWAYS true for a
        # promoted row, so it kept getting re-selected as "due" forever.
        # A later re-probe returning no hit (a transient failure, a rate
        # limit, anything) then overwrote review_status back to
        # 'no_match'/'rejected' for a company that was, by then, a
        # confirmed ACTIVE source -- corrupting the audit trail (the
        # actual `sources` row and its data were unaffected, this only
        # broke discovery_candidates' own record of what happened).
        cur.execute(
            "SELECT id, company, ats, config FROM discovery_candidates "
            "WHERE review_status = 'unchecked' "
            "   OR (review_status IN ('no_match', 'rejected') AND next_check_at <= now()) "
            "ORDER BY next_check_at LIMIT %s FOR UPDATE SKIP LOCKED",
            (limit,),
        )
        due = cur.fetchall()

    if not due:
        return []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(_process_candidate, due))


def _process_disabled_source(row: dict) -> dict:
    connector_cls = CONNECTORS.get(row["ats"])
    try:
        trial_postings = connector_cls().fetch(row["config"]) if connector_cls else []
        verdict = verify_trial_fetch(row["company"], trial_postings)
    except Exception as exc:  # noqa: BLE001
        verdict = {"passed": False, "reason": f"trial fetch raised: {exc}", "evidence": {}}

    if verdict["passed"]:
        with cursor() as cur:
            cur.execute(
                "UPDATE sources SET status = 'probation', consecutive_failures = 0, "
                "next_scrape_at = now() WHERE id = %s",
                (row["id"],),
            )
            cur.execute(
                "INSERT INTO events (kind, company, detail) VALUES ('reinstated', %s, %s)",
                (row["company"], "recovered after being disabled, back to probation"),
            )
        return {"company": row["company"], "outcome": "reinstated_to_probation"}
    return {"company": row["company"], "outcome": "still_broken", "reason": verdict["reason"]}


def recheck_disabled_sources(limit: int = 5, max_workers: int = DISCOVERY_CANDIDATE_WORKERS) -> list[dict]:
    """Self-healing half: give a disabled source one trial fetch through
    the SAME gate a new candidate has to pass. A pass sends it back to
    probation (re-earns its second confirmation), not straight to
    active -- one working fetch after a string of failures could itself
    be a fluke. Each disabled source is a REAL connector fetch (not a
    cheap probe), so unlike run_discovery_cycle this rarely has enough
    volume for concurrency to matter much in practice -- parallelized
    anyway for consistency and because it costs nothing when the list
    is short."""
    with cursor() as cur:
        cur.execute(
            "SELECT id, company, ats, config FROM sources WHERE status = 'disabled' "
            "ORDER BY last_scraped_at LIMIT %s FOR UPDATE SKIP LOCKED",
            (limit,),
        )
        disabled = cur.fetchall()

    if not disabled:
        return []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(_process_disabled_source, disabled))


# Sized for a real ~10K-candidate queue (SEC EDGAR's full company list),
# not the original 9-name placeholder. limit=5 every 30 min would have
# taken roughly 87 DAYS to grind through 10,391 names once -- each probe
# is a handful of quick HTTP checks against a DIFFERENT company's own
# domain (no shared site being hammered, unlike a single source's job-
# A fixed sleep between cycles was fine when each cycle was slow anyway
# (sequential probing dominated the wall time). It stops being fine once
# probing is fast: confirmed live, the ORIGINAL sequential+TIMEOUT=12
# setup averaged 5.8s/candidate, so a 150-candidate batch alone took
# ~14.5 minutes -- longer than the 5-minute sleep, so the sleep barely
# mattered. With per-candidate latency fixed (Workday parallelized,
# TIMEOUT=5, 24-way candidate concurrency), that same batch finishes in
# roughly 150/24 * ~2s ≈ 12 SECONDS -- if run_forever still slept 300s
# after every batch regardless, the sleep would become the new
# bottleneck by two orders of magnitude, undoing most of the fix. See
# run_forever's own "drain, then idle" logic below for how this is
# actually handled: a FULL batch (there's more backlog waiting) loops
# again immediately with no sleep; sleep only happens once a cycle comes
# back with fewer than `batch_size` results, meaning the current due
# queue is actually drained, not just this cycle's slice of it.
DISCOVERY_POLL_INTERVAL_SECONDS = 300
DISCOVERY_BATCH_SIZE = 300


def run_forever(poll_interval: int = DISCOVERY_POLL_INTERVAL_SECONDS, batch_size: int = DISCOVERY_BATCH_SIZE) -> None:
    import time

    print(f"Discovery loop starting: batch size {batch_size}, idle poll {poll_interval}s.", flush=True)
    while True:
        discovery_results = run_discovery_cycle(limit=batch_size)
        for r in discovery_results:
            print(f"[discovery] {r}", flush=True)
        recheck_results = recheck_disabled_sources(limit=batch_size)
        for r in recheck_results:
            print(f"[recheck]   {r}", flush=True)

        # Full batch on EITHER call means there's likely more backlog
        # right behind it -- keep draining without sleeping. Only idle
        # once a cycle comes back with less than a full batch, meaning
        # the current due queue (unchecked + anything whose next_check_at
        # has come due) is actually empty for now.
        drained = len(discovery_results) < batch_size and len(recheck_results) < batch_size
        if drained:
            time.sleep(poll_interval)


if __name__ == "__main__":
    try:
        run_forever()
    except KeyboardInterrupt:
        pass
