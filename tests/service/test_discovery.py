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


def test_candidate_with_no_ats_hit_marked_no_match(monkeypatch):
    monkeypatch.setattr(discovery, "CANDIDATE_SEED", ["Nobody Corp"])
    monkeypatch.setattr(discovery, "PROBES", [])
    monkeypatch.setattr(discovery, "_probe_jsonld", lambda company, domain: None)

    results = discovery.run_discovery_cycle(limit=5)
    assert results == [{"company": "Nobody Corp", "outcome": "no_match"}]

    with db.cursor() as cur:
        cur.execute("SELECT review_status FROM discovery_candidates WHERE company = 'Nobody Corp'")
        assert cur.fetchone()["review_status"] == "no_match"


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
