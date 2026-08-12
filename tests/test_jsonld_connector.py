import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from connectors.jsonld import JsonLdConnector  # noqa: E402


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


SITEMAP_INDEX = """<sitemapindex>
<sitemap><loc>https://example.com/sitemap1.xml</loc></sitemap>
<sitemap><loc>https://example.com/sitemap2.xml</loc></sitemap>
</sitemapindex>"""

SUB_SITEMAP_1 = """<urlset>
<url><loc>https://example.com/job/1</loc></url>
<url><loc>https://example.com/job/2</loc></url>
</urlset>"""

SUB_SITEMAP_2 = """<urlset>
<url><loc>https://example.com/job/3</loc></url>
</urlset>"""

JOB_PAGE = """<html><script type="application/ld+json">
{"@type": "JobPosting", "title": "Test Intern", "identifier": {"value": "R1"},
 "hiringOrganization": {"name": "Example Co"}, "jobLocation": {"address": {"addressLocality": "Austin"}}}
</script></html>"""


def test_sitemap_index_is_expanded_one_level():
    # Regression: RTX's real sitemap is a sitemap-index (9 sub-sitemaps),
    # not a flat list of job URLs — the connector originally treated the
    # index's own <loc> entries (more .xml files) as if they were job
    # pages and found nothing.
    responses = {
        "https://example.com/sitemap_index.xml": FakeResponse(SITEMAP_INDEX),
        "https://example.com/sitemap1.xml": FakeResponse(SUB_SITEMAP_1),
        "https://example.com/sitemap2.xml": FakeResponse(SUB_SITEMAP_2),
        "https://example.com/job/1": FakeResponse(JOB_PAGE),
        "https://example.com/job/2": FakeResponse(JOB_PAGE),
        "https://example.com/job/3": FakeResponse(JOB_PAGE),
    }

    def fake_get(url, timeout=None, headers=None):
        return responses[url]

    with patch("connectors.jsonld.requests.get", side_effect=fake_get), patch("connectors.jsonld.time.sleep"):
        entry = {
            "company": "Example Co",
            "sitemap_url": "https://example.com/sitemap_index.xml",
            "url_pattern": "/job/",
        }
        postings = JsonLdConnector().fetch(entry)

    assert len(postings) == 3


def test_flat_sitemap_still_works_without_an_index():
    responses = {
        "https://example.com/sitemap.xml": FakeResponse(SUB_SITEMAP_1),
        "https://example.com/job/1": FakeResponse(JOB_PAGE),
        "https://example.com/job/2": FakeResponse(JOB_PAGE),
    }

    def fake_get(url, timeout=None, headers=None):
        return responses[url]

    with patch("connectors.jsonld.requests.get", side_effect=fake_get), patch("connectors.jsonld.time.sleep"):
        entry = {
            "company": "Example Co",
            "sitemap_url": "https://example.com/sitemap.xml",
            "url_pattern": "/job/",
        }
        postings = JsonLdConnector().fetch(entry)

    assert len(postings) == 2
