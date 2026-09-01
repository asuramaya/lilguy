import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ, reason="needs a scratch Postgres via DATABASE_URL"
)

import db  # noqa: E402
import discovery  # noqa: E402


def fake_posting(id_, company, title):
    return SimpleNamespace(id=id_, company=company, title=title, location="Remote", url=f"https://x/{id_}",
                            source="greenhouse", category="Test", posted_at=None, description_snippet="")


@pytest.fixture(autouse=True)
def _clean_db():
    db.init_schema()
    with db.cursor() as cur:
        cur.execute("TRUNCATE postings, scrape_runs, sources, discovery_candidates RESTART IDENTITY CASCADE")
    yield


@pytest.fixture(autouse=True)
def _no_real_commoncrawl_calls(monkeypatch):
    # run_discovery_cycle() unconditionally calls
    # _seed_commoncrawl_candidates_if_due() -- without this, the FIRST
    # test in the whole suite to call run_discovery_cycle() would trigger
    # a REAL network call to Common Crawl (the interval gate's
    # `_last_commoncrawl_seed_at` starts as None), making every test run
    # depend on network access and Common Crawl's uptime. Pretending it
    # "just ran" makes the gate skip it by default; a specific test that
    # wants to exercise the seeding path itself resets this back to None.
    import datetime as _datetime
    monkeypatch.setattr(discovery, "_last_commoncrawl_seed_at", _datetime.datetime.now(_datetime.timezone.utc))
    yield


def test_candidate_with_no_ats_hit_marked_no_match(monkeypatch):
    monkeypatch.setattr(discovery, "CANDIDATE_SEED", ["Nobody Corp"])
    monkeypatch.setattr(discovery, "PROBES", [])
    monkeypatch.setattr(discovery, "_probe_jsonld", lambda company, domain: None)

    results = discovery.run_discovery_cycle(limit=5)
    assert results == [{"company": "Nobody Corp", "outcome": "no_match"}]

    with db.cursor() as cur:
        cur.execute("SELECT review_status FROM discovery_candidates WHERE company = 'Nobody Corp'")
        assert cur.fetchone()["review_status"] == "no_match"


def test_probe_exception_is_recorded_as_no_match_not_a_crash(monkeypatch):
    # Regression: a garbage candidate name (a Wikipedia "List of ..."
    # meta-article that slipped through candidate_sources.py's filtering)
    # slugified into an overlong, invalid domain label. requests.get()
    # raised urllib3.exceptions.LocationParseError for it -- NOT a
    # requests.RequestException -- which propagated out of the worker
    # thread, crashed run_forever()'s while loop, and Docker's restart
    # policy brought the process back up to re-claim and re-crash on the
    # SAME row every time (confirmed live in production, 2026-08-16).
    # probe_candidate() itself is patched to actually raise here (not
    # just return None) -- the assertion is that _process_candidate
    # catches it and keeps going, not that a specific probe declines.
    monkeypatch.setattr(discovery, "CANDIDATE_SEED", ["List of Nonsense Companies"])

    def exploding_probe(company):
        raise Exception("simulated LocationParseError: label empty or too long")

    monkeypatch.setattr(discovery, "probe_candidate", exploding_probe)

    results = discovery.run_discovery_cycle(limit=5)
    assert len(results) == 1
    assert results[0]["company"] == "List of Nonsense Companies"
    assert results[0]["outcome"] == "no_match"

    with db.cursor() as cur:
        cur.execute(
            "SELECT review_status, next_check_at FROM discovery_candidates WHERE company = %s",
            ("List of Nonsense Companies",),
        )
        row = cur.fetchone()
        assert row["review_status"] == "no_match"
        assert row["next_check_at"] is not None  # pushed out, not immediately due again


def test_candidate_that_passes_verification_goes_to_probation(monkeypatch):
    monkeypatch.setattr(discovery, "CANDIDATE_SEED", ["Acme Corp"])

    def fake_greenhouse_probe(company):
        return {"ats": "greenhouse", "config": {"company": company, "ats": "greenhouse", "token": "acme"}}

    monkeypatch.setattr(discovery, "PROBES", [fake_greenhouse_probe])
    real_postings = [fake_posting("gh:acme:1", "Acme Corp", "Supply Chain Intern"),
                      fake_posting("gh:acme:2", "Acme Corp", "Finance Intern")]
    monkeypatch.setitem(discovery.CONNECTORS, "greenhouse",
                         lambda: SimpleNamespace(fetch=lambda cfg: real_postings))

    results = discovery.run_discovery_cycle(limit=5)
    assert results == [{"company": "Acme Corp", "outcome": "promoted_to_probation", "ats": "greenhouse"}]

    with db.cursor() as cur:
        cur.execute("SELECT status, added_by FROM sources WHERE company = 'Acme Corp'")
        row = cur.fetchone()
    assert row["status"] == "probation"
    assert row["added_by"] == "discovery"


def test_promoted_candidate_is_never_re_selected_as_due(monkeypatch):
    # Regression: the promotion UPDATE never set next_check_at, which
    # defaults to now() at insert time -- "due if next_check_at <= now()"
    # was therefore ALWAYS true for a promoted row, so a second
    # run_discovery_cycle() call would re-probe it, and any transient
    # no-hit result overwrote 'promoted' back to 'no_match'/'rejected'
    # for a company that was by then a confirmed active source. Caught
    # live by restoring a real backup and finding confirmed-active
    # companies (Penumbra Inc, Shield AI, VTEX) mislabeled 'no_match' in
    # discovery_candidates despite `sources.status = 'active'` being
    # correct the whole time.
    monkeypatch.setattr(discovery, "CANDIDATE_SEED", ["Acme Corp"])

    def fake_greenhouse_probe(company):
        return {"ats": "greenhouse", "config": {"company": company, "ats": "greenhouse", "token": "acme"}}

    monkeypatch.setattr(discovery, "PROBES", [fake_greenhouse_probe])
    real_postings = [fake_posting("gh:acme:1", "Acme Corp", "Supply Chain Intern"),
                      fake_posting("gh:acme:2", "Acme Corp", "Finance Intern")]
    monkeypatch.setitem(discovery.CONNECTORS, "greenhouse",
                         lambda: SimpleNamespace(fetch=lambda cfg: real_postings))

    first = discovery.run_discovery_cycle(limit=5)
    assert first == [{"company": "Acme Corp", "outcome": "promoted_to_probation", "ats": "greenhouse"}]

    # If the promoted probe (still wired to succeed) got called again,
    # a second cycle would report 'promoted_to_probation' again too --
    # the real assertion is that it ISN'T selected as due at all.
    second = discovery.run_discovery_cycle(limit=5)
    assert second == []

    with db.cursor() as cur:
        cur.execute("SELECT review_status FROM discovery_candidates WHERE company = 'Acme Corp'")
        assert cur.fetchone()["review_status"] == "promoted"


def test_guess_domains_tries_root_and_careers_subdomain():
    # Confirmed live (task #23) that careers.{domain}.com is a common real
    # pattern the original single-guess version entirely missed -- three
    # of this project's own existing sources (PepsiCo, Honeywell, General
    # Mills) all resolve there.
    assert discovery._guess_domains("Acme Corp") == ["acmecorp.com", "careers.acmecorp.com"]


def test_probe_candidate_falls_back_through_both_domain_guesses(monkeypatch):
    monkeypatch.setattr(discovery, "PROBES", [])
    seen_domains = []

    def fake_jsonld(company, domain):
        seen_domains.append(domain)
        return {"ats": "jsonld", "config": {}} if domain == "careers.acmecorp.com" else None

    monkeypatch.setattr(discovery, "_probe_jsonld", fake_jsonld)
    hit = discovery.probe_candidate("Acme Corp")
    assert seen_domains == ["acmecorp.com", "careers.acmecorp.com"]
    assert hit == {"ats": "jsonld", "config": {}}


def test_preresolved_candidate_skips_probing_and_goes_straight_to_trial_fetch(monkeypatch):
    # Common Crawl's Workday tenant/host/site triples (see
    # candidate_sources.py) get seeded with ats/config already populated
    # -- there's nothing left to guess, so _process_candidate should go
    # straight to a trial fetch through that exact config. PROBES is set
    # to a function that raises if called at all, so this test fails
    # loudly if that guarantee ever regresses.
    def exploding_probe(company):
        raise AssertionError("a pre-resolved candidate must not be probed")

    monkeypatch.setattr(discovery, "PROBES", [exploding_probe])
    monkeypatch.setattr(discovery, "CANDIDATE_SEED", [])  # nothing else to seed this cycle

    config = {"company": "acme", "ats": "workday", "tenant": "acme", "wd_host": "wd1",
              "site": "Search", "category": "Uncategorized", "max_pages": 5}
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO discovery_candidates (company, ats, config) VALUES (%s, %s, %s)",
            ("acme", "workday", __import__("psycopg2").extras.Json(config)),
        )

    real_postings = [fake_posting("wd:acme:1", "acme", "Supply Chain Intern"),
                      fake_posting("wd:acme:2", "acme", "Logistics Intern")]
    monkeypatch.setitem(discovery.CONNECTORS, "workday",
                         lambda: SimpleNamespace(fetch=lambda cfg: real_postings if cfg == config else []))

    results = discovery.run_discovery_cycle(limit=5)
    assert results == [{"company": "acme", "outcome": "promoted_to_probation", "ats": "workday"}]

    with db.cursor() as cur:
        cur.execute("SELECT status, added_by FROM sources WHERE company = 'acme'")
        row = cur.fetchone()
    assert row["status"] == "probation"
    assert row["added_by"] == "discovery"


def _stub_commoncrawl(monkeypatch, **overrides):
    """Stubs EVERY Common Crawl fetcher, then applies the overrides.

    Patching only the fetchers a test cares about leaves the others
    making real network calls: adding the Ashby and SmartRecruiters
    fetchers turned this suite from 27s into 232s and seeded it with
    thousands of real tokens before anyone noticed. Defaulting all of
    them to empty means a fetcher added later fails loudly as a missing
    attribute rather than quietly reaching the internet.
    """
    monkeypatch.setattr(discovery, "_last_commoncrawl_seed_at", None)  # force the gate open
    for name in ("fetch_commoncrawl_greenhouse_tokens", "fetch_commoncrawl_ashby_tokens",
                 "fetch_commoncrawl_smartrecruiters_tokens", "fetch_commoncrawl_workable_tokens",
                 "fetch_commoncrawl_bamboohr_tokens", "fetch_commoncrawl_icims_slugs"):
        monkeypatch.setattr(discovery, name, overrides.pop(name, lambda: []))
    for name in ("fetch_commoncrawl_workday_tenants", "fetch_commoncrawl_taleo_tenants",
                 "fetch_commoncrawl_ukg_boards"):
        monkeypatch.setattr(discovery, name, overrides.pop(name, lambda: []))
    assert not overrides, f"unknown fetcher(s): {sorted(overrides)}"


def test_commoncrawl_seeding_inserts_plain_names_and_preresolved_workday_rows(monkeypatch):
    _stub_commoncrawl(
        monkeypatch,
        fetch_commoncrawl_greenhouse_tokens=lambda: ["acme", "beta"],
        fetch_commoncrawl_workday_tenants=lambda: [{"tenant": "gamma", "wd_host": "wd1", "site": "Search"}],
    )

    discovery._seed_commoncrawl_candidates_if_due()

    with db.cursor() as cur:
        cur.execute("SELECT company, ats, config, review_status FROM discovery_candidates ORDER BY company")
        rows = cur.fetchall()

    by_company = {r["company"]: r for r in rows}
    assert set(by_company) == {"acme", "beta", "gamma"}
    assert by_company["acme"]["ats"] is None  # plain name, same shape as SEC EDGAR/Wikipedia seeds
    assert by_company["gamma"]["ats"] == "workday"
    assert by_company["gamma"]["config"]["wd_host"] == "wd1"
    assert by_company["gamma"]["config"]["site"] == "Search"
    assert all(r["review_status"] == "unchecked" for r in rows)

    # The interval gate: calling again immediately must NOT re-fetch (the
    # mocked fetch functions would return the same data harmlessly here,
    # but in production this is what keeps a slow multi-page Common Crawl
    # pull from stalling every 5-minute discovery cycle).
    monkeypatch.setattr(discovery, "fetch_commoncrawl_greenhouse_tokens",
                         lambda: (_ for _ in ()).throw(AssertionError("should not be called again so soon")))
    discovery._seed_commoncrawl_candidates_if_due()


def test_commoncrawl_seeding_skips_candidates_that_case_insensitively_collide_with_an_existing_source(monkeypatch):
    # Regression: confirmed live this session -- Common Crawl's real
    # Workday pull found tenant "3m" (lowercase, straight from the real
    # URL), a DIFFERENT string than the existing manual source "3M".
    # sources' UNIQUE (company, ats) constraint is case-sensitive, so
    # without this guard "3m"/workday would promote into a second,
    # redundant source scraping the exact same board as the existing "3M".
    _stub_commoncrawl(
        monkeypatch,
        fetch_commoncrawl_greenhouse_tokens=lambda: ["flexport", "newco"],
        fetch_commoncrawl_workday_tenants=lambda: [
            {"tenant": "3m", "wd_host": "wd1", "site": "Search"},
            {"tenant": "newtenant", "wd_host": "wd1", "site": "Careers"}],
    )
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (company, ats, category, config, status) VALUES "
            "('3M', 'workday', 'Industrial Manufacturing', '{}', 'active'), "
            "('Flexport', 'greenhouse', 'Freight Forwarding', '{}', 'active')"
        )

    discovery._seed_commoncrawl_candidates_if_due()

    with db.cursor() as cur:
        cur.execute("SELECT company FROM discovery_candidates")
        seeded = {r["company"] for r in cur.fetchall()}
    assert seeded == {"newco", "newtenant"}  # "3m" and "flexport" excluded, already covered by existing sources


def test_candidate_that_fails_verification_is_rejected_not_promoted(monkeypatch):
    # Simulates a tenant/site guess that resolves cleanly (a real HTTP
    # hit) but belongs to a different company -- the exact failure mode
    # that made a review-free auto-promote risky without this gate.
    monkeypatch.setattr(discovery, "CANDIDATE_SEED", ["Acme Corp"])

    def fake_workday_probe(company):
        return {"ats": "workday", "config": {"company": company, "ats": "workday", "tenant": "wrong"}}

    monkeypatch.setattr(discovery, "PROBES", [fake_workday_probe])
    wrong_company_postings = [fake_posting("wd:wrong:1", "Some Unrelated Company", "Marketing Intern")]
    monkeypatch.setitem(discovery.CONNECTORS, "workday",
                         lambda: SimpleNamespace(fetch=lambda cfg: wrong_company_postings))

    results = discovery.run_discovery_cycle(limit=5)
    assert results[0]["outcome"] == "rejected"

    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM sources WHERE company = 'Acme Corp'")
        assert cur.fetchone()["n"] == 0


def test_disabled_source_reinstated_to_probation_on_recovery(monkeypatch):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (company, ats, config, status) VALUES ('Acme Corp', 'greenhouse', %s, 'disabled')",
            (__import__("psycopg2").extras.Json({"company": "Acme Corp", "ats": "greenhouse", "token": "acme"}),),
        )

    recovered = [fake_posting("gh:acme:1", "Acme Corp", "Supply Chain Intern")]
    monkeypatch.setitem(discovery.CONNECTORS, "greenhouse", lambda: SimpleNamespace(fetch=lambda cfg: recovered))

    results = discovery.recheck_disabled_sources(limit=5)
    assert results == [{"company": "Acme Corp", "outcome": "reinstated_to_probation"}]

    with db.cursor() as cur:
        cur.execute("SELECT status FROM sources WHERE company = 'Acme Corp'")
        assert cur.fetchone()["status"] == "probation"


def test_recheck_cadence_is_shortest_for_a_board_that_simply_had_no_interns():
    # The dominant rejection (3538 of 4441 live rows) is a board that
    # worked fine and just had nothing intern-shaped posted yet -- a
    # statement about when we looked, not about the source. It must come
    # back around far sooner than a structurally-broken candidate, or a
    # 90-day clock steps over whole recruiting windows.
    assert discovery._recheck_days("no_internship_titles") == 14
    assert discovery._recheck_days("zero_postings") == 30
    assert discovery._recheck_days("fetch_error") == 30
    assert discovery._recheck_days("not_distinct") == 90
    assert discovery._recheck_days("name_mismatch") == 90


def test_unknown_or_missing_code_falls_back_to_the_conservative_interval():
    # Old rows predate `code`, and a future verify.py could add a verdict
    # this table doesn't know about; neither should re-probe aggressively.
    assert discovery._recheck_days("something_new") == discovery.REJECTED_RECHECK_DAYS
    assert discovery._recheck_days("") == discovery.REJECTED_RECHECK_DAYS


def test_ashby_and_smartrecruiters_seed_as_plain_candidate_names(monkeypatch):
    # Neither needs a pre-resolved config the way Workday's
    # tenant/host/site triple does: _probe_ashby slugifies and
    # _probe_smartrecruiters tries the company's own capitalisation, so
    # a bare token is enough.
    _stub_commoncrawl(
        monkeypatch,
        fetch_commoncrawl_ashby_tokens=lambda: ["ramp", "linear"],
        fetch_commoncrawl_smartrecruiters_tokens=lambda: ["Visa", "Bosch"],
    )
    discovery._seed_commoncrawl_candidates_if_due()

    with db.cursor() as cur:
        cur.execute("SELECT company, ats FROM discovery_candidates")
        rows = cur.fetchall()
    assert {r["company"] for r in rows} == {"ramp", "linear", "Visa", "Bosch"}
    assert all(r["ats"] is None for r in rows), "plain names, not pre-resolved configs"


def test_smartrecruiters_candidate_case_is_preserved(monkeypatch):
    # Its company identifier is CASE-SENSITIVE, and a lowercased one
    # returns an empty list rather than a 404 -- a miss indistinguishable
    # from a company with no openings. Folding case here would silently
    # discard almost every real board.
    _stub_commoncrawl(monkeypatch, fetch_commoncrawl_smartrecruiters_tokens=lambda: ["Visa"])
    discovery._seed_commoncrawl_candidates_if_due()
    with db.cursor() as cur:
        cur.execute("SELECT company FROM discovery_candidates")
        assert [r["company"] for r in cur.fetchall()] == ["Visa"]


def test_workable_bamboohr_icims_seed_as_plain_candidate_names(monkeypatch):
    # Same reasoning as Ashby/SmartRecruiters above: each probe already
    # tries the given string as-is (hyphenated or slugified), so a bare
    # real token needs no pre-resolved config.
    _stub_commoncrawl(
        monkeypatch,
        fetch_commoncrawl_workable_tokens=lambda: ["back-market"],
        fetch_commoncrawl_bamboohr_tokens=lambda: ["helpscout"],
        fetch_commoncrawl_icims_slugs=lambda: ["federatedinsurance"],
    )
    discovery._seed_commoncrawl_candidates_if_due()

    with db.cursor() as cur:
        cur.execute("SELECT company, ats FROM discovery_candidates")
        rows = cur.fetchall()
    assert {r["company"] for r in rows} == {"back-market", "helpscout", "federatedinsurance"}
    assert all(r["ats"] is None for r in rows), "plain names, not pre-resolved configs"


def test_taleo_seeds_as_preresolved_tenant_section_pairs(monkeypatch):
    # Sidesteps _probe_taleo()'s TALEO_SECTION_GUESSES entirely -- a real
    # crawled (tenant, section) pair needs no guessing.
    _stub_commoncrawl(
        monkeypatch,
        fetch_commoncrawl_taleo_tenants=lambda: [{"tenant": "wipo", "section": "wp_internship"}],
    )
    discovery._seed_commoncrawl_candidates_if_due()

    with db.cursor() as cur:
        cur.execute("SELECT company, ats, config FROM discovery_candidates")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["company"] == "wipo"
    assert rows[0]["ats"] == "taleo"
    assert rows[0]["config"]["section"] == "wp_internship"


def test_ukg_seeds_as_preresolved_host_tenant_board_triples(monkeypatch):
    # UKG has no guessing probe at all -- board_id is an opaque GUID --
    # so Common Crawl is the only path a UKG source can ever be found.
    _stub_commoncrawl(
        monkeypatch,
        fetch_commoncrawl_ukg_boards=lambda: [
            {"host": "recruiting.ultipro.com", "tenant": "ACM1000ACME", "board_id": "86df2700-c124-49b9-b096-7cacea55e9dd"}
        ],
    )
    discovery._seed_commoncrawl_candidates_if_due()

    with db.cursor() as cur:
        cur.execute("SELECT company, ats, config FROM discovery_candidates")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["company"] == "ACM1000ACME"
    assert rows[0]["ats"] == "ukg"
    assert rows[0]["config"]["board_id"] == "86df2700-c124-49b9-b096-7cacea55e9dd"


def test_a_failing_commoncrawl_fetcher_does_not_stop_the_others(monkeypatch):
    def boom():
        raise RuntimeError("Common Crawl is down")

    _stub_commoncrawl(
        monkeypatch,
        fetch_commoncrawl_ashby_tokens=boom,
        fetch_commoncrawl_smartrecruiters_tokens=lambda: ["Visa"],
    )
    discovery._seed_commoncrawl_candidates_if_due()
    with db.cursor() as cur:
        cur.execute("SELECT company FROM discovery_candidates")
        assert [r["company"] for r in cur.fetchall()] == ["Visa"]


def _candidate(company, review_status="no_match", version=0, next_check_days=90):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO discovery_candidates (company, review_status, probe_set_version, "
            "                                  checked_at, next_check_at) "
            "VALUES (%s, %s, %s, now(), now() + make_interval(days => %s)) RETURNING id",
            (company, review_status, version, next_check_days),
        )
        return cur.fetchone()["id"]


def _due_companies(limit=10):
    """What run_discovery_cycle would pick up, without running probes."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT company FROM discovery_candidates "
            "WHERE review_status = 'unchecked' "
            "   OR (review_status IN ('no_match', 'rejected') "
            "       AND (next_check_at <= now() OR probe_set_version < %s)) "
            "ORDER BY (review_status = 'unchecked') DESC, "
            "         (probe_set_version >= %s) DESC, next_check_at "
            "LIMIT %s",
            (discovery.PROBE_SET_VERSION, discovery.PROBE_SET_VERSION, limit),
        )
        return [r["company"] for r in cur.fetchall()]


def test_a_candidate_judged_by_an_older_probe_set_becomes_due_again():
    # "No ATS found" is a claim about the PROBE SET, not the company.
    # Confirmed live: "ramp" is a real Ashby board with a live
    # internship, judged no_match two days before Ashby shipped and
    # parked for 90 days on the strength of that verdict.
    _candidate("stale", version=discovery.PROBE_SET_VERSION - 1, next_check_days=90)
    assert "stale" in _due_companies()


def test_a_candidate_judged_by_the_current_probe_set_keeps_its_cooldown():
    # Otherwise every reseed re-probes the same tokens forever.
    _candidate("current", version=discovery.PROBE_SET_VERSION, next_check_days=90)
    assert _due_companies() == []


def test_never_checked_candidates_are_not_buried_behind_the_re_examination():
    # An unchecked row also carries version 0, so a naive "current
    # version first" rule would put every genuinely new candidate behind
    # the entire backlog.
    for i in range(3):
        _candidate(f"stale{i}", version=discovery.PROBE_SET_VERSION - 1, next_check_days=90)
    _candidate("brand-new", review_status="unchecked", version=0, next_check_days=0)
    assert _due_companies(limit=1) == ["brand-new"]


def test_a_promoted_candidate_is_never_re_examined_by_a_version_bump():
    # Its fate lives in sources.status from promotion onward; re-probing
    # it could overwrite the audit trail for an active source.
    _candidate("already-live", review_status="promoted",
               version=discovery.PROBE_SET_VERSION - 1, next_check_days=90)
    assert _due_companies() == []
