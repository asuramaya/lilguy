import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))
from connectors.rippling import RipplingConnector
from work_arrangement import REMOTE, HYBRID, ONSITE


def test_rippling_missing_token_raises():
    c = RipplingConnector()
    with pytest.raises(ValueError, match="missing 'token'"):
        c.fetch({"company": "Acme"})


def test_rippling_parse_jobs(monkeypatch):
    mock_data = [
        {
            "id": "job_123",
            "name": "Software Engineering Intern",
            "location": {"city": "San Francisco", "state": "CA", "country": "USA"},
            "workplaceType": "hybrid",
            "created_at": "2026-08-20T12:00:00Z",
            "description": "<p>Build great features.</p>",
            "url": "https://ats.rippling.com/acme/jobs/job_123"
        },
        {
            "id": "job_456",
            "title": "Remote Data Analyst Intern",
            "location": "Remote",
            "workplaceType": "remote",
            "description": "Analyze datasets.",
        }
    ]

    monkeypatch.setattr(
        "requests.get",
        lambda url, **kw: SimpleNamespace(
            status_code=200,
            json=lambda: mock_data,
            raise_for_status=lambda: None
        )
    )

    c = RipplingConnector()
    postings = c.fetch({"ats": "rippling", "company": "Acme Corp", "token": "acme", "category": "Tech"})
    assert len(postings) == 2
    
    p1 = postings[0]
    assert p1.id == "rippling:acme:job_123"
    assert p1.company == "Acme Corp"
    assert p1.title == "Software Engineering Intern"
    assert p1.location == "San Francisco, CA, USA"
    assert p1.work_arrangement == HYBRID
    assert p1.source == "rippling"
    assert "Build great features." in p1.description

    p2 = postings[1]
    assert p2.id == "rippling:acme:job_456"
    assert p2.work_arrangement == REMOTE
