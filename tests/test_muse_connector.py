import sys
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from connectors.muse import MuseConnector  # noqa: E402


def _job(job_id, name="Some Intern Role", categories=None):
    return {
        "id": job_id,
        "name": name,
        "company": {"name": "Example Co"},
        "locations": [],
        "categories": [{"name": c} for c in (categories or [])],
        "refs": {"landing_page": f"https://example.com/jobs/{job_id}"},
        "publication_date": "2026-01-01",
        "contents": "",
    }


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_categories_list_dedups_a_job_appearing_in_multiple_categories():
    # A real job can legitimately be tagged under more than one Muse
    # category (e.g. a "Business Operations" role also tagged "Sales") —
    # querying both categories separately must not double-count it.
    shared_job = _job("shared-1", categories=["Business Operations", "Sales"])
    only_ops_job = _job("ops-only", categories=["Business Operations"])

    def fake_get(url, params, timeout):
        category = params.get("category")
        if category == "Business Operations":
            return FakeResponse({"total": 2, "page_count": 1, "results": [shared_job, only_ops_job]})
        if category == "Sales":
            return FakeResponse({"total": 1, "page_count": 1, "results": [shared_job]})
        raise AssertionError(f"unexpected category {category}")

    with patch("connectors.muse.requests.get", side_effect=fake_get):
        entry = {"categories": ["Business Operations", "Sales"], "level": "Internship"}
        postings = MuseConnector().fetch(entry)

    ids = sorted(p.id for p in postings)
    assert ids == ["muse:ops-only", "muse:shared-1"]


def test_single_category_still_works_without_a_list():
    def fake_get(url, params, timeout):
        assert params.get("category") == "Business Operations"
        return FakeResponse({"total": 1, "page_count": 1, "results": [_job("a")]})

    with patch("connectors.muse.requests.get", side_effect=fake_get):
        entry = {"category": "Business Operations", "level": "Internship"}
        postings = MuseConnector().fetch(entry)

    assert len(postings) == 1


def test_no_category_pulls_everything_blended():
    def fake_get(url, params, timeout):
        assert "category" not in params
        return FakeResponse({"total": 1, "page_count": 1, "results": [_job("a")]})

    with patch("connectors.muse.requests.get", side_effect=fake_get):
        entry = {"level": "Internship"}
        postings = MuseConnector().fetch(entry)

    assert len(postings) == 1


def test_one_failed_category_does_not_lose_the_others():
    # Regression: confirmed live that a single transient network failure
    # inside one category's request used to raise out of fetch()
    # entirely, discarding every OTHER category's already-successful
    # results too — which store.py's rebuild() then read as "all of
    # these postings closed" and nearly wiped the raw store in one run.
    def fake_get(url, params, timeout):
        category = params.get("category")
        if category == "Sales":
            raise requests.exceptions.ReadTimeout("simulated transient failure")
        return FakeResponse({"total": 1, "page_count": 1, "results": [_job(f"{category}-1")]})

    with patch("connectors.muse.requests.get", side_effect=fake_get), patch("connectors.muse.time.sleep"):
        entry = {"categories": ["Business Operations", "Sales", "Data and Analytics"], "level": "Internship"}
        postings = MuseConnector().fetch(entry)

    ids = sorted(p.id for p in postings)
    assert ids == ["muse:Business Operations-1", "muse:Data and Analytics-1"]


def test_all_categories_failing_still_raises():
    # A genuinely broken config (every category erroring) should still
    # fail loudly, not silently return an empty list indistinguishable
    # from "no postings right now."
    def fake_get(url, params, timeout):
        raise requests.exceptions.ConnectionError("simulated total outage")

    with patch("connectors.muse.requests.get", side_effect=fake_get), patch("connectors.muse.time.sleep"):
        entry = {"categories": ["Business Operations", "Sales"], "level": "Internship"}
        try:
            MuseConnector().fetch(entry)
            assert False, "expected a ValueError"
        except ValueError as exc:
            assert "every category failed" in str(exc)


def test_transient_failure_recovers_via_retry():
    calls = {"n": 0}

    def fake_get(url, params, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ReadTimeout("simulated transient failure")
        return FakeResponse({"total": 1, "page_count": 1, "results": [_job("a")]})

    with patch("connectors.muse.requests.get", side_effect=fake_get), patch("connectors.muse.time.sleep"):
        entry = {"category": "Business Operations", "level": "Internship"}
        postings = MuseConnector().fetch(entry)

    assert len(postings) == 1
    assert calls["n"] == 2  # failed once, succeeded on retry
