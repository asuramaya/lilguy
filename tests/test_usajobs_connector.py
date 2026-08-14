import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from connectors.usajobs import UsaJobsConnector  # noqa: E402

# NOTE: this response shape is built from USAJobs' PUBLIC API
# documentation, not confirmed against a real live 200 response (this
# project holds no API key — see usajobs.py's own docstring). These
# tests prove the connector correctly parses that DOCUMENTED shape and
# paginates correctly; they do NOT prove the documented shape matches
# reality. Whoever obtains a real key should re-verify field-by-field
# against a live call before this connector is added to sources.yaml.


def _item(job_id, title="Supply Chain Intern"):
    return {
        "MatchedObjectId": job_id,
        "MatchedObjectDescriptor": {
            "PositionID": job_id,
            "PositionTitle": title,
            "OrganizationName": "Department of Transportation",
            "PositionLocation": [{"LocationName": "Washington, DC"}],
            "PositionURI": f"https://www.usajobs.gov/job/{job_id}",
            "PublicationStartDate": "2026-08-01",
            "UserArea": {"Details": {"JobSummary": "A federal internship opportunity."}},
        },
    }


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)[:300]

    def json(self):
        return self._payload


def _search_result(items, total):
    return {"SearchResult": {"SearchResultItems": items, "SearchResultCountAll": total}}


def test_missing_credentials_raises_clearly():
    connector = UsaJobsConnector()
    with patch.dict("os.environ", {}, clear=True):
        try:
            connector.fetch({"keyword": "intern"})
            assert False, "expected a ValueError"
        except ValueError as exc:
            assert "USAJOBS_API_KEY" in str(exc)


def test_parses_documented_response_shape():
    items = [_item("1"), _item("2", title="Logistics Intern")]

    def fake_get(url, params, headers, timeout):
        return FakeResponse(_search_result(items, total=2))

    env = {"USAJOBS_API_KEY": "fake-key", "USAJOBS_USER_AGENT": "test@example.org"}
    with patch("connectors.usajobs.requests.get", side_effect=fake_get), patch.dict("os.environ", env):
        postings = UsaJobsConnector().fetch({"keyword": "intern"})

    assert len(postings) == 2
    assert postings[0].company == "Department of Transportation"
    assert postings[0].location == "Washington, DC"
    assert postings[1].title == "Logistics Intern"


def test_stops_when_fewer_than_a_full_page_returned():
    def fake_get(url, params, headers, timeout):
        return FakeResponse(_search_result([_item("1")], total=1))

    env = {"USAJOBS_API_KEY": "fake-key", "USAJOBS_USER_AGENT": "test@example.org"}
    with patch("connectors.usajobs.requests.get", side_effect=fake_get) as mock_get, patch.dict("os.environ", env):
        postings = UsaJobsConnector().fetch({"keyword": "intern"})

    assert len(postings) == 1
    assert mock_get.call_count == 1  # didn't try a second page for a single-result total


def test_bad_status_code_raises_with_context():
    def fake_get(url, params, headers, timeout):
        return FakeResponse({"error": "nope"}, status_code=500)

    env = {"USAJOBS_API_KEY": "fake-key", "USAJOBS_USER_AGENT": "test@example.org"}
    with patch("connectors.usajobs.requests.get", side_effect=fake_get), patch.dict("os.environ", env):
        try:
            UsaJobsConnector().fetch({"keyword": "intern"})
            assert False, "expected a ValueError"
        except ValueError as exc:
            assert "500" in str(exc)
