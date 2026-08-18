"""SmartRecruiters' list response carries NO description, so this
connector deliberately leaves description unset (stored as NULL, "not
fetched yet") rather than putting the N+1 detail fetch on the scrape
path -- the shape that deadlocked the Workday backfill.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from connectors.smartrecruiters import SmartRecruitersConnector, _location  # noqa: E402

ENTRY = {"ats": "smartrecruiters", "company": "Visa", "token": "Visa", "category": "Payments Technology"}

# Trimmed from a real live response.
JOB = {
    "id": "744000133907678",
    "name": "Sr. Manager",
    "releasedDate": "2026-06-24T10:00:11.853Z",
    "location": {"city": "Austin", "region": "TX", "country": "us", "remote": False},
    "function": {"id": "engineering", "label": "Engineering"},
    "industry": {"id": "it_and_services", "label": "Information Technology And Services"},
}


def _connector(pages, monkeypatch, status=200):
    calls = []

    def fake_get(url, params=None, **kw):
        calls.append(params or {})
        payload = pages[min(len(calls) - 1, len(pages) - 1)]
        return SimpleNamespace(status_code=status, json=lambda: payload,
                               raise_for_status=lambda: None)

    monkeypatch.setattr("connectors.smartrecruiters.requests.get", fake_get)
    return SmartRecruitersConnector(), calls


def test_maps_a_live_posting(monkeypatch):
    c, _ = _connector([{"content": [JOB], "totalFound": 1}], monkeypatch)
    [p] = c.fetch(ENTRY)
    assert p.id == "smartrecruiters:Visa:744000133907678"
    assert p.title == "Sr. Manager"
    assert p.location == "Austin, TX, US"
    assert p.url == "https://jobs.smartrecruiters.com/Visa/744000133907678"
    assert p.posted_at == "2026-06-24T10:00:11.853Z"


def test_job_function_comes_from_the_platform_but_industry_does_not(monkeypatch):
    # The posting carries an `industry` label too, but writing it into
    # `category` would reintroduce a third taxonomy into the column that
    # two of them were just disentangled from. The curated sources.yaml
    # industry wins; the function label is used because job_function had
    # no vocabulary at all on direct boards before this.
    c, _ = _connector([{"content": [JOB], "totalFound": 1}], monkeypatch)
    [p] = c.fetch(ENTRY)
    assert p.job_function == "Engineering"
    assert p.category == "Payments Technology"
    assert "Information Technology" not in p.category


def test_description_is_left_unset_for_a_later_backfill(monkeypatch):
    c, _ = _connector([{"content": [JOB], "totalFound": 1}], monkeypatch)
    [p] = c.fetch(ENTRY)
    assert p.description == ""
    assert p.description_snippet == ""


def test_pagination_walks_until_totalfound_is_reached(monkeypatch):
    page1 = {"content": [{**JOB, "id": f"a{i}"} for i in range(100)], "totalFound": 150}
    page2 = {"content": [{**JOB, "id": f"b{i}"} for i in range(50)], "totalFound": 150}
    c, calls = _connector([page1, page2], monkeypatch)
    assert len(c.fetch(ENTRY)) == 150
    assert [c_["offset"] for c_ in calls] == [0, 100]


def test_pagination_stops_rather_than_running_to_max_pages(monkeypatch):
    c, calls = _connector([{"content": [JOB], "totalFound": 1}], monkeypatch)
    c.fetch({**ENTRY, "max_pages": 10})
    assert len(calls) == 1


def test_a_missing_token_is_a_config_error(monkeypatch):
    c, _ = _connector([{"content": []}], monkeypatch)
    with pytest.raises(ValueError, match="missing 'token'"):
        c.fetch({"ats": "smartrecruiters", "company": "Visa"})


def test_404_names_the_token(monkeypatch):
    c, _ = _connector([{}], monkeypatch, status=404)
    with pytest.raises(ValueError, match="smartrecruiters token 'Visa'"):
        c.fetch(ENTRY)


def test_an_unexpected_shape_is_rejected_not_silently_empty(monkeypatch):
    c, _ = _connector([{"totalFound": 0}], monkeypatch)
    with pytest.raises(ValueError, match="unexpected smartrecruiters response shape"):
        c.fetch(ENTRY)


@pytest.mark.parametrize("location, expected", [
    ({"city": "Austin", "region": "TX", "country": "us"}, "Austin, TX, US"),
    ({"city": "Berlin", "country": "de"}, "Berlin, DE"),
    ({"country": "us"}, "US"),
    ({"city": "Austin", "region": "TX", "country": "us", "remote": True}, "Remote - Austin, TX, US"),
    ({"remote": True}, "Remote"),
    ({}, ""),
    (None, ""),
])
def test_location_is_assembled_from_the_parts_that_exist(location, expected):
    # Country is upper-cased because the API returns a lowercase code,
    # which reads as a typo next to "Austin, TX".
    assert _location(location) == expected
