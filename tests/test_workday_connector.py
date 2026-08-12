import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from connectors.workday import WorkdayConnector  # noqa: E402


def _job(n):
    return {"title": f"Job {n}", "externalPath": f"/job/job-{n}", "locationsText": "", "postedOn": ""}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_pagination_survives_total_resetting_to_zero_after_page_one():
    # Regression: confirmed live against a real Workday tenant (Unilever)
    # that `total` is only accurate on the FIRST page of a query and
    # reports 0 on every page after, even while still returning full
    # batches of real results. A loop that re-reads `total` each page
    # (the original bug) stops after page 2 regardless of how many
    # postings actually exist.
    pages = [
        {"total": 45, "jobPostings": [_job(i) for i in range(20)]},
        {"total": 0, "jobPostings": [_job(i) for i in range(20, 40)]},
        {"total": 0, "jobPostings": [_job(i) for i in range(40, 45)]},
    ]

    def fake_post(url, json, headers, timeout):
        offset = json["offset"]
        page_index = offset // 20
        return FakeResponse(pages[page_index])

    with patch("connectors.workday.requests.post", side_effect=fake_post):
        entry = {"company": "Test Co", "tenant": "test", "wd_host": "wd1", "site": "Careers"}
        postings = WorkdayConnector().fetch(entry)

    assert len(postings) == 45


def test_stops_on_empty_batch_even_if_total_says_more():
    # Safety net for the opposite failure direction: total overstates
    # what's actually available.
    pages = [
        {"total": 1000, "jobPostings": [_job(i) for i in range(20)]},
        {"total": 0, "jobPostings": []},
    ]

    def fake_post(url, json, headers, timeout):
        offset = json["offset"]
        page_index = offset // 20
        return FakeResponse(pages[page_index])

    with patch("connectors.workday.requests.post", side_effect=fake_post):
        entry = {"company": "Test Co", "tenant": "test", "wd_host": "wd1", "site": "Careers"}
        postings = WorkdayConnector().fetch(entry)

    assert len(postings) == 20
