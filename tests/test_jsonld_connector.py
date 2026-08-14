import sys
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from connectors.jsonld import JsonLdConnector  # noqa: E402


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


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

JOB_PAGE_NESTED_COUNTRY = """<html><script type="application/ld+json">
{"@type": "JobPosting", "title": "Nested Country Intern", "identifier": {"value": "R2"},
 "hiringOrganization": {"name": "Example Co"},
 "jobLocation": {"address": {"addressLocality": "Turin", "addressRegion": "Piedmont",
                              "addressCountry": {"@type": "Country", "name": "IT"}}}}
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


def test_sitemap_index_urls_with_query_strings_still_detected():
    # Regression: Eaton's real sitemap-index URLs carry a trailing query
    # string (".../sitemap.xml?domain=eaton.com") — a naive
    # `.endswith(".xml")` check is always False against those, so the
    # index was never expanded and its own 2 URLs were read as if they
    # were job pages (0 real postings found).
    index = """<sitemapindex>
<sitemap><loc>https://example.com/sitemap.xml?domain=example.com</loc></sitemap>
<sitemap><loc>https://example.com/sitemap_cat.xml?domain=example.com</loc></sitemap>
</sitemapindex>"""
    jobs = """<urlset>
<url><loc>https://example.com/job/1?domain=example.com</loc></url>
</urlset>"""
    cats = """<urlset>
<url><loc>https://example.com/category/ops?domain=example.com</loc></url>
</urlset>"""
    responses = {
        "https://example.com/sitemap_index.xml?domain=example.com": FakeResponse(index),
        "https://example.com/sitemap.xml?domain=example.com": FakeResponse(jobs),
        "https://example.com/sitemap_cat.xml?domain=example.com": FakeResponse(cats),
        "https://example.com/job/1?domain=example.com": FakeResponse(JOB_PAGE),
    }

    def fake_get(url, timeout=None, headers=None):
        return responses[url]

    with patch("connectors.jsonld.requests.get", side_effect=fake_get), patch("connectors.jsonld.time.sleep"):
        entry = {
            "company": "Example Co",
            "sitemap_url": "https://example.com/sitemap_index.xml?domain=example.com",
            "url_pattern": "/job/",
        }
        postings = JsonLdConnector().fetch(entry)

    assert len(postings) == 1


def test_sitemap_fetch_recovers_via_retry():
    # Regression: RTX's sub-sitemap fetch hit a transient 403 even with
    # request pacing in place, which killed the whole source since
    # _collect_locs had no retry (unlike _fetch_one, which already
    # tolerates one dead job-page link).
    calls = {"n": 0}

    def fake_get(url, timeout=None, headers=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse("forbidden", status_code=403)
        return FakeResponse(SUB_SITEMAP_1)

    with patch("connectors.jsonld.requests.get", side_effect=fake_get), patch("connectors.jsonld.time.sleep"):
        locs = JsonLdConnector()._collect_locs("https://example.com/sitemap.xml")

    assert calls["n"] == 2
    assert locs == ["https://example.com/job/1", "https://example.com/job/2"]


def test_sitemap_fetch_raises_after_repeated_failure():
    def fake_get(url, timeout=None, headers=None):
        return FakeResponse("forbidden", status_code=403)

    with patch("connectors.jsonld.requests.get", side_effect=fake_get), patch("connectors.jsonld.time.sleep"):
        raised = False
        try:
            JsonLdConnector()._collect_locs("https://example.com/sitemap.xml")
        except Exception:
            raised = True
        assert raised


def test_nested_address_country_object_does_not_crash():
    # Regression: Eaton's Eightfold-hosted JobPosting JSON-LD nests
    # addressCountry as a {"@type": "Country", "name": "IT"} object
    # rather than a plain string — a raw ", ".join over the address
    # fields crashed with a TypeError instead of producing a location.
    responses = {
        "https://example.com/sitemap.xml": FakeResponse(
            '<urlset><url><loc>https://example.com/job/1</loc></url></urlset>'
        ),
        "https://example.com/job/1": FakeResponse(JOB_PAGE_NESTED_COUNTRY),
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

    assert len(postings) == 1
    assert postings[0].location == "Turin, Piedmont, IT"


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
