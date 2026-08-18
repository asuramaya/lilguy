"""Ashby's list response carries descriptions, which is the property that
makes this connector immune to the failure mode that deadlocked the
Workday description backfill.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from connectors.ashby import AshbyConnector  # noqa: E402

ENTRY = {"ats": "ashby", "company": "Ramp", "token": "ramp", "category": "Fintech"}

# Trimmed from a real live response (api.ashbyhq.com/posting-api/job-board/ramp).
JOB = {
    "id": "34413f8d-26bf-4bbc-8ade-eb309a0e2245",
    "title": " Security Engineer, Cloud",
    "location": "New York, NY (HQ)",
    "publishedAt": "2026-04-07T17:12:35.753+00:00",
    "isListed": True,
    "isRemote": True,
    "jobUrl": "https://jobs.ashbyhq.com/ramp/34413f8d",
    "applyUrl": "https://jobs.ashbyhq.com/ramp/34413f8d/application",
    "descriptionHtml": "<h1>About Ramp</h1><p>Smart infrastructure for finance teams.</p>",
    "descriptionPlain": "ABOUT RAMP\n\nSmart infrastructure for finance teams.",
}


def _connector(payload, status=200, monkeypatch=None):
    def fake_get(url, **kw):
        return SimpleNamespace(status_code=status, json=lambda: payload,
                               raise_for_status=lambda: None)
    monkeypatch.setattr("connectors.ashby.requests.get", fake_get)
    return AshbyConnector()


def test_maps_a_live_posting(monkeypatch):
    c = _connector({"jobs": [JOB]}, monkeypatch=monkeypatch)
    [p] = c.fetch(ENTRY)
    assert p.id == "ashby:ramp:34413f8d-26bf-4bbc-8ade-eb309a0e2245"
    assert p.title == "Security Engineer, Cloud", "leading whitespace must be trimmed"
    assert p.company == "Ramp"
    assert p.location == "New York, NY (HQ)"
    assert p.url == "https://jobs.ashbyhq.com/ramp/34413f8d"
    assert p.source == "ashby"
    assert p.category == "Fintech"
    assert p.posted_at == "2026-04-07T17:12:35.753+00:00"


def test_description_arrives_with_the_list_and_needs_no_second_fetch(monkeypatch):
    # The whole reason to prefer this connector: Workday's list response
    # carries no description, which forces a per-posting fetch, which is
    # where the backfill deadlock came from.
    c = _connector({"jobs": [JOB]}, monkeypatch=monkeypatch)
    [p] = c.fetch(ENTRY)
    assert "Smart infrastructure for finance teams" in p.description
    assert "<h1>" not in p.description, "markup must not reach the display text"
    assert p.description_snippet.startswith("ABOUT RAMP")


def test_unlisted_jobs_are_skipped(monkeypatch):
    # isListed false means the employer is not advertising it. Showing it
    # would present as open something they chose to hide.
    hidden = {**JOB, "id": "hidden", "isListed": False}
    c = _connector({"jobs": [JOB, hidden]}, monkeypatch=monkeypatch)
    assert [p.id.split(":")[-1] for p in c.fetch(ENTRY)] == [JOB["id"]]


def test_a_job_with_no_id_is_skipped_rather_than_given_a_broken_one(monkeypatch):
    c = _connector({"jobs": [{**JOB, "id": None}]}, monkeypatch=monkeypatch)
    assert c.fetch(ENTRY) == []


def test_a_missing_token_is_a_config_error(monkeypatch):
    c = _connector({"jobs": []}, monkeypatch=monkeypatch)
    with pytest.raises(ValueError, match="missing 'token'"):
        c.fetch({"ats": "ashby", "company": "Ramp"})


def test_404_names_the_token_rather_than_raising_something_opaque(monkeypatch):
    c = _connector({}, status=404, monkeypatch=monkeypatch)
    with pytest.raises(ValueError, match="ashby token 'ramp'"):
        c.fetch(ENTRY)


def test_an_unexpected_shape_is_rejected_not_silently_empty(monkeypatch):
    c = _connector([], monkeypatch=monkeypatch)
    with pytest.raises(ValueError, match="unexpected ashby response shape"):
        c.fetch(ENTRY)
