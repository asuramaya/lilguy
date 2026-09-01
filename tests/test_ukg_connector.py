import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))
from connectors.ukg import UkgConnector


def test_ukg_missing_fields_raises():
    c = UkgConnector()
    with pytest.raises(ValueError, match="missing"):
        c.fetch({"company": "Acme"})


def test_ukg_bad_board_raises(monkeypatch):
    monkeypatch.setattr(
        "requests.post",
        lambda *a, **kw: SimpleNamespace(status_code=404, json=lambda: {}),
    )
    c = UkgConnector()
    with pytest.raises(ValueError, match="returned HTTP 404"):
        c.fetch({"company": "Acme", "host": "recruiting.ultipro.com", "tenant": "ACM1000ACME", "board_id": "abc"})


def test_ukg_empty_body_is_not_mistaken_for_zero_postings(monkeypatch):
    # Confirmed live: a bare {} request body silently returns
    # totalCount=0 on a board that has real open postings -- this
    # connector must always send the nested opportunitySearch shape.
    seen_bodies = []

    def fake_post(url, json=None, headers=None, timeout=None):
        seen_bodies.append(json)
        return SimpleNamespace(status_code=200, json=lambda: {
            "totalCount": 1,
            "opportunities": [{
                "Id": "job-1",
                "Title": "Software Engineering Intern",
                "Locations": [{"Address": {"City": "Austin", "State": {"Code": "TX"}, "Country": {"Code": "USA"}}}],
                "BriefDescription": "Build things.",
                "PostedDate": "2026-08-01",
            }],
        })

    monkeypatch.setattr("requests.post", fake_post)
    c = UkgConnector()
    postings = c.fetch({"company": "Acme Corp", "host": "recruiting.ultipro.com",
                          "tenant": "ACM1000ACME", "board_id": "abc-123", "category": "Tech"})

    assert seen_bodies[0] == {"opportunitySearch": {"Top": 50, "Skip": 0}}
    assert len(postings) == 1
    p = postings[0]
    assert p.id == "ukg:ACM1000ACME:job-1"
    assert p.title == "Software Engineering Intern"
    assert p.location == "Austin, TX, USA"
    assert "Build things." in p.description
    assert p.url == "https://recruiting.ultipro.com/ACM1000ACME/JobBoard/abc-123/OpportunityDetail?opportunityId=job-1"


def test_ukg_paginates_until_total_reached(monkeypatch):
    # A real response never returns fewer than Top unless it's the last
    # page -- 50 items on page one, the remaining 25 on page two.
    page_1 = {"totalCount": 75, "opportunities": [{"Id": str(i), "Title": f"Intern {i}"} for i in range(50)]}
    page_2 = {"totalCount": 75, "opportunities": [{"Id": str(i), "Title": f"Intern {i}"} for i in range(50, 75)]}
    pages = [page_1, page_2]
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        return SimpleNamespace(status_code=200, json=lambda: pages[len(calls) - 1])

    monkeypatch.setattr("requests.post", fake_post)
    c = UkgConnector()
    postings = c.fetch({"company": "Acme", "host": "recruiting.ultipro.com", "tenant": "ACM1000ACME",
                          "board_id": "abc-123", "max_pages": 60})
    assert len(postings) == 75
    assert calls[0]["opportunitySearch"]["Skip"] == 0
    assert calls[1]["opportunitySearch"]["Skip"] == 50
