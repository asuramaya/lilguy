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
SEC EDGAR's own public company-tickers list -- 10,391 real company
names, free, no API key, no signup (see candidate_sources.py) -- rather
than a hand-typed list someone has to keep extending by hand. That list
IS still a human-curated input in one sense (someone chose "public
companies with a US ticker" as the pool), but growing it from here on
is a research/infrastructure question (which free, public bulk list to
add next), not a per-company chore.
"""
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import psycopg2.extras
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent))

from connectors import CONNECTORS  # noqa: E402

from candidate_sources import fetch_sec_edgar_company_names, fetch_wikipedia_category_companies  # noqa: E402
from db import cursor  # noqa: E402
from verify import verify_trial_fetch  # noqa: E402

TIMEOUT = 12
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


def _guess_domain(company: str) -> str:
    return f"{_slugify(company)}.com"


def _probe_greenhouse(company: str):
    token = _slugify(company)
    try:
        r = requests.get(GREENHOUSE_API.format(token=token), timeout=TIMEOUT, headers=UA)
        if r.status_code == 200 and r.json().get("jobs"):
            return {"ats": "greenhouse", "config": {"company": company, "ats": "greenhouse", "token": token,
                                                      "category": "Uncategorized", "max_pages": TRIAL_MAX_PAGES}}
    except (requests.RequestException, ValueError):
        pass
    return None


def _probe_lever(company: str):
    token = _slugify(company)
    try:
        r = requests.get(LEVER_API.format(token=token), timeout=TIMEOUT, headers=UA)
        if r.status_code == 200 and isinstance(r.json(), list) and r.json():
            return {"ats": "lever", "config": {"company": company, "ats": "lever", "token": token,
                                                 "category": "Uncategorized", "max_pages": TRIAL_MAX_PAGES}}
    except (requests.RequestException, ValueError):
        pass
    return None


def _probe_workday(company: str):
    tenant = _slugify(company)
    for host in WORKDAY_HOST_GUESSES:
        for site in WORKDAY_SITE_GUESSES:
            url = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
            try:
                r = requests.post(url, json={"limit": 1, "offset": 0, "searchText": ""}, timeout=TIMEOUT,
                                   headers={"Content-Type": "application/json"})
                if r.status_code == 200 and "jobPostings" in r.json():
                    return {"ats": "workday", "config": {"company": company, "ats": "workday", "tenant": tenant,
                                                            "wd_host": host, "site": site,
                                                            "category": "Uncategorized", "max_pages": 5}}
            except (requests.RequestException, ValueError):
                continue
    return None


def _fetch_sitemap_locs(url: str):
    try:
        r = requests.get(url, timeout=TIMEOUT, headers=UA)
        if r.status_code == 200:
            return re.findall(r"<loc>([^<]+)</loc>", r.text)
    except requests.RequestException:
        pass
    return []


def _probe_jsonld(company: str, domain: str):
    try:
        r = requests.get(f"https://{domain}/robots.txt", timeout=TIMEOUT, headers=UA)
    except requests.RequestException:
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
    guess and is the least reliable of the four, so it goes last."""
    for probe in PROBES:
        hit = probe(company)
        if hit:
            return hit
    return _probe_jsonld(company, _guess_domain(company))


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


def run_discovery_cycle(limit: int = 5) -> list[dict]:
    """Processes up to `limit` due candidates. Returns a result summary
    per candidate for logging."""
    _seed_unchecked_candidates()
    results = []

    with cursor() as cur:
        cur.execute(
            "SELECT id, company FROM discovery_candidates "
            "WHERE review_status IN ('unchecked') OR next_check_at <= now() "
            "ORDER BY next_check_at LIMIT %s FOR UPDATE SKIP LOCKED",
            (limit,),
        )
        due = cur.fetchall()

    for row in due:
        company = row["company"]
        now = datetime.now(timezone.utc)
        hit = probe_candidate(company)

        if hit is None:
            with cursor() as cur:
                cur.execute(
                    "UPDATE discovery_candidates SET review_status = 'no_match', checked_at = %s, "
                    "next_check_at = %s WHERE id = %s",
                    (now, now + timedelta(days=NO_MATCH_RECHECK_DAYS), row["id"]),
                )
            results.append({"company": company, "outcome": "no_match"})
            continue

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
                results.append({"company": company, "outcome": "promoted_to_probation", "ats": hit["ats"]})
            else:
                cur.execute(
                    "UPDATE discovery_candidates SET ats = %s, config = %s, review_status = 'rejected', "
                    "evidence = %s, checked_at = %s, next_check_at = %s WHERE id = %s",
                    (hit["ats"], psycopg2.extras.Json(hit["config"]),
                     psycopg2.extras.Json({**verdict["evidence"], "reason": verdict["reason"]}),
                     now, now + timedelta(days=REJECTED_RECHECK_DAYS), row["id"]),
                )
                results.append({"company": company, "outcome": "rejected", "reason": verdict["reason"]})

    return results


def recheck_disabled_sources(limit: int = 5) -> list[dict]:
    """Self-healing half: give a disabled source one trial fetch through
    the SAME gate a new candidate has to pass. A pass sends it back to
    probation (re-earns its second confirmation), not straight to
    active -- one working fetch after a string of failures could itself
    be a fluke."""
    with cursor() as cur:
        cur.execute(
            "SELECT id, company, ats, config FROM sources WHERE status = 'disabled' "
            "ORDER BY last_scraped_at LIMIT %s FOR UPDATE SKIP LOCKED",
            (limit,),
        )
        disabled = cur.fetchall()

    results = []
    for row in disabled:
        connector_cls = CONNECTORS.get(row["ats"])
        try:
            trial_postings = connector_cls().fetch(row["config"]) if connector_cls else []
            verdict = verify_trial_fetch(row["company"], trial_postings)
        except Exception as exc:  # noqa: BLE001
            verdict = {"passed": False, "reason": f"trial fetch raised: {exc}", "evidence": {}}

        with cursor() as cur:
            if verdict["passed"]:
                cur.execute(
                    "UPDATE sources SET status = 'probation', consecutive_failures = 0, "
                    "next_scrape_at = now() WHERE id = %s",
                    (row["id"],),
                )
                results.append({"company": row["company"], "outcome": "reinstated_to_probation"})
            else:
                results.append({"company": row["company"], "outcome": "still_broken", "reason": verdict["reason"]})

    return results


# Sized for a real ~10K-candidate queue (SEC EDGAR's full company list),
# not the original 9-name placeholder. limit=5 every 30 min would have
# taken roughly 87 DAYS to grind through 10,391 names once -- each probe
# is a handful of quick HTTP checks against a DIFFERENT company's own
# domain (no shared site being hammered, unlike a single source's job-
# page pacing), so a much higher per-cycle batch is safe. At limit=150 /
# 5 min this clears the initial backlog in a few hours, then settles
# into steady-state re-checking of no_match/rejected candidates on their
# own long (90-day) intervals.
DISCOVERY_POLL_INTERVAL_SECONDS = 300
DISCOVERY_BATCH_SIZE = 150


def run_forever(poll_interval: int = DISCOVERY_POLL_INTERVAL_SECONDS, batch_size: int = DISCOVERY_BATCH_SIZE) -> None:
    import time

    print(f"Discovery loop starting: polling every {poll_interval}s, batch size {batch_size}.", flush=True)
    while True:
        for r in run_discovery_cycle(limit=batch_size):
            print(f"[discovery] {r}", flush=True)
        for r in recheck_disabled_sources(limit=batch_size):
            print(f"[recheck]   {r}", flush=True)
        time.sleep(poll_interval)


if __name__ == "__main__":
    try:
        run_forever()
    except KeyboardInterrupt:
        pass
