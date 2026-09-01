import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))
from connectors.bamboohr import BambooHRConnector


def test_bamboohr_missing_token_raises():
    c = BambooHRConnector()
    with pytest.raises(ValueError, match="missing 'token'"):
        c.fetch({"company": "Acme"})


def test_bamboohr_bad_token_raises(monkeypatch):
    # A wrong token 200s (redirected to bamboohr.com's own marketing
    # site) rather than 404ing -- confirmed live -- so the connector must
    # detect this via the missing `result` key, not the status code.
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: {"marketing": True}, raise_for_status=lambda: None),
    )
    c = BambooHRConnector()
    with pytest.raises(ValueError, match="doesn't look like a real board"):
        c.fetch({"company": "Acme", "token": "acme"})


def test_bamboohr_parse_jobs(monkeypatch):
    mock_data = {
        "meta": {"totalCount": 2},
        "result": [
            {
                "id": 111,
                "jobOpeningName": "Marketing Intern",
                "location": {"city": "Salt Lake City", "state": "UT", "country": "USA"},
                "description": "<p>Help with campaigns.</p>",
                "postedDate": "2026-08-01",
            },
            {
                "id": 222,
                "jobOpeningName": "Remote Support Intern",
                "isRemote": True,
                "description": "Support customers.",
            },
        ],
    }
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: mock_data, raise_for_status=lambda: None),
    )

    c = BambooHRConnector()
    postings = c.fetch({"ats": "bamboohr", "company": "Acme Corp", "token": "acme", "category": "Tech"})
    assert len(postings) == 2

    p1 = postings[0]
    assert p1.id == "bamboohr:acme:111"
    assert p1.title == "Marketing Intern"
    assert p1.location == "Salt Lake City, UT, USA"
    assert "Help with campaigns." in p1.description

    p2 = postings[1]
    assert p2.id == "bamboohr:acme:222"
    assert p2.location == "Remote"
