import sys
import time
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))
from connectors.workable import WorkableConnector


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)


def test_workable_missing_token_raises():
    c = WorkableConnector()
    with pytest.raises(ValueError, match="missing 'token'"):
        c.fetch({"company": "Acme"})


def test_workable_404_raises(monkeypatch):
    monkeypatch.setattr(
        "requests.post",
        lambda *a, **kw: SimpleNamespace(status_code=404, json=lambda: {}, raise_for_status=lambda: None),
    )
    c = WorkableConnector()
    with pytest.raises(ValueError, match="returned 404"):
        c.fetch({"company": "Acme", "token": "acme"})


def test_workable_parse_jobs(monkeypatch):
    mock_list = {
        "total": 2,
        "results": [
            {
                "shortcode": "ABC123",
                "title": "Software Engineering Intern",
                "city": "San Francisco",
                "state": "CA",
                "country": "USA",
                "published_on": "2026-08-20",
                "url": "https://apply.workable.com/acme/j/ABC123/",
            },
            {
                "shortcode": "DEF456",
                "title": "Remote Data Analyst Intern",
                "telecommuting": True,
            },
        ],
    }

    def fake_post(url, json=None, headers=None, timeout=None):
        assert "Referer" in headers  # WAF requires this, confirmed live
        return SimpleNamespace(status_code=200, json=lambda: mock_list, raise_for_status=lambda: None)

    def fake_get(url, headers=None, timeout=None):
        if "ABC123" in url:
            return SimpleNamespace(status_code=200, json=lambda: {"description": "<p>Build great features.</p>"})
        return SimpleNamespace(status_code=404, json=lambda: {})

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("requests.get", fake_get)

    c = WorkableConnector()
    postings = c.fetch({"ats": "workable", "company": "Acme Corp", "token": "acme", "category": "Tech"})
    assert len(postings) == 2

    p1 = postings[0]
    assert p1.id == "workable:acme:ABC123"
    assert p1.title == "Software Engineering Intern"
    assert p1.location == "San Francisco, CA, USA"
    assert "Build great features." in p1.description

    p2 = postings[1]
    assert p2.id == "workable:acme:DEF456"
    assert p2.location == "Remote"
    assert p2.description == ""  # detail fetch 404'd -- blank, not a crash
