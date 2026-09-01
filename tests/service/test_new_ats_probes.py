"""Unit tests for the probe functions added for rippling/workable/bamboohr/
taleo/icims (PROBE_SET_VERSION 3). Network-free -- monkeypatches
requests.get directly, unlike tests/service/test_discovery.py's
DB-backed run_discovery_cycle() tests, so these don't need DATABASE_URL.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

import discovery  # noqa: E402


def test_probe_rippling_hit(monkeypatch):
    monkeypatch.setattr(
        discovery.requests, "get",
        lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: [{"id": "1", "name": "Intern"}]),
    )
    hit = discovery._probe_rippling("Acme Corp")
    assert hit["ats"] == "rippling"
    assert hit["config"]["token"] == "acmecorp"


def test_probe_rippling_miss(monkeypatch):
    monkeypatch.setattr(discovery.requests, "get", lambda *a, **kw: SimpleNamespace(status_code=404))
    assert discovery._probe_rippling("Nobody Corp") is None


def test_probe_workable_tries_hyphenated_token_first(monkeypatch):
    seen_tokens = []

    def fake_post(url, json=None, headers=None, timeout=None):
        seen_tokens.append(headers.get("Referer"))
        if "back-market" in url:
            return SimpleNamespace(status_code=200, json=lambda: {"results": [{"title": "Intern"}]})
        return SimpleNamespace(status_code=404)

    monkeypatch.setattr(discovery.requests, "post", fake_post)
    hit = discovery._probe_workable("Back Market")
    assert hit["ats"] == "workable"
    assert hit["config"]["token"] == "back-market"
    assert seen_tokens[0] == "https://apply.workable.com/back-market/"


def test_probe_workable_empty_jobs_is_a_miss(monkeypatch):
    monkeypatch.setattr(
        discovery.requests, "post",
        lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: {"results": []}),
    )
    assert discovery._probe_workable("Acme") is None


def test_probe_bamboohr_hit(monkeypatch):
    monkeypatch.setattr(
        discovery.requests, "get",
        lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: {"result": [{"id": 1}]}),
    )
    hit = discovery._probe_bamboohr("Acme Corp")
    assert hit["ats"] == "bamboohr"


def test_probe_bamboohr_wrong_token_is_a_miss(monkeypatch):
    # A wrong token 200s (redirected to the marketing site) rather than
    # 404ing -- confirmed live -- so the probe must key off the missing
    # `result` field, not the status code.
    monkeypatch.setattr(
        discovery.requests, "get",
        lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: {"marketing": True}),
    )
    assert discovery._probe_bamboohr("Acme") is None


def test_probe_taleo_hit_on_second_section_guess(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        if "/careersection/2/" in url:
            return SimpleNamespace(status_code=200, text="Career Section Unavailable")
        if "/careersection/1/" in url:
            return SimpleNamespace(status_code=200, text="queryString: 'portal=999'")
        return SimpleNamespace(status_code=404, text="")

    monkeypatch.setattr(discovery.requests, "get", fake_get)
    hit = discovery._probe_taleo("Acme Corp")
    assert hit["ats"] == "taleo"
    assert hit["config"]["section"] == "1"
    assert hit["config"]["tenant"] == "acmecorp"


def test_probe_taleo_all_sections_miss(monkeypatch):
    monkeypatch.setattr(discovery.requests, "get", lambda *a, **kw: SimpleNamespace(status_code=404, text=""))
    assert discovery._probe_taleo("Nobody Corp") is None


def test_probe_icims_hit(monkeypatch):
    monkeypatch.setattr(
        discovery.requests, "get",
        lambda *a, **kw: SimpleNamespace(status_code=200, text='<li class="iCIMS_JobCardItem">...'),
    )
    hit = discovery._probe_icims("Acme Corp")
    assert hit["ats"] == "icims"
    assert hit["config"]["slug"] == "acmecorp"


def test_probe_icims_no_jobs_is_a_miss(monkeypatch):
    monkeypatch.setattr(
        discovery.requests, "get",
        lambda *a, **kw: SimpleNamespace(status_code=200, text="<html>no jobs here</html>"),
    )
    assert discovery._probe_icims("Acme Corp") is None
