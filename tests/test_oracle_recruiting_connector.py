import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from connectors.oracle_recruiting import OracleRecruitingConnector  # noqa: E402


def _req(job_id, title="Intern Role"):
    return {
        "Id": job_id,
        "Title": title,
        "PrimaryLocation": "Austin, TX",
        "PostedDate": "2026-08-01",
        "ShortDescriptionStr": "A great opportunity.",
        "ExternalQualificationsStr": None,
        "ExternalResponsibilitiesStr": None,
    }


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_paginates_via_offset_until_total_reached():
    # The connector's own page size is 25 (limit=25 baked into fetch()),
    # so a total of 30 spans exactly two pages: 25 + 5.
    TOTAL = 30

    def fake_get(url, params, timeout, headers):
        finder = params["finder"]
        offset = int([p for p in finder.split(",") if p.startswith("offset=")][0].split("=")[1])
        remaining = max(0, TOTAL - offset)
        batch = [_req(offset + i) for i in range(min(25, remaining))]
        return FakeResponse({"items": [{"TotalJobsCount": TOTAL, "requisitionList": batch}]})

    with patch("connectors.oracle_recruiting.requests.get", side_effect=fake_get):
        entry = {
            "company": "Example Co",
            "host": "example.fa.ocs.oraclecloud.com",
            "site_number": "CX_1",
            "job_detail_base": "https://careers.example.com/job",
        }
        postings = OracleRecruitingConnector().fetch(entry)

    assert len(postings) == TOTAL
    assert postings[0].url == "https://careers.example.com/job/0"
    assert postings[0].id == "oracle_recruiting:example.fa.ocs.oraclecloud.com:0"


def test_missing_required_fields_raises_clearly():
    try:
        OracleRecruitingConnector().fetch({"company": "Example Co"})
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "host" in str(exc) and "site_number" in str(exc) and "job_detail_base" in str(exc)


def test_bad_status_code_raises_with_context():
    def fake_get(url, params, timeout, headers):
        return FakeResponse({}, status_code=500)

    with patch("connectors.oracle_recruiting.requests.get", side_effect=fake_get):
        entry = {
            "company": "Example Co",
            "host": "example.fa.ocs.oraclecloud.com",
            "site_number": "CX_1",
            "job_detail_base": "https://careers.example.com/job",
        }
        try:
            OracleRecruitingConnector().fetch(entry)
            assert False, "expected ValueError"
        except ValueError as exc:
            assert "500" in str(exc)
