import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

from candidate_sources import (  # noqa: E402
    _latest_commoncrawl_index,
    fetch_commoncrawl_bamboohr_tokens,
    fetch_commoncrawl_greenhouse_tokens,
    fetch_commoncrawl_icims_slugs,
    fetch_commoncrawl_taleo_tenants,
    fetch_commoncrawl_workable_tokens,
    fetch_commoncrawl_workday_tenants,
    fetch_sec_edgar_company_names,
    fetch_wikipedia_category_companies,
)


class FakeResponse:
    """`text`/`json()` shaped like whichever real endpoint is being faked
    -- collinfo.json and the CDX showNumPages meta call are real JSON
    objects, but the CDX page results themselves are JSONL (one JSON
    object per line), consumed via `.text.splitlines()`, not `.json()`
    (confirmed live -- the CDX endpoint's `output=json` param means
    "each line is JSON", not "the whole body is one JSON document")."""
    def __init__(self, payload=None, text=None):
        self._payload = payload
        self._text = text

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload

    @property
    def text(self):
        return self._text if self._text is not None else json.dumps(self._payload)


def _cdx_jsonl(*urls):
    return "\n".join(json.dumps({"url": u}) for u in urls)


def test_sec_edgar_extracts_titles_from_indexed_dict():
    payload = {
        "0": {"cik_str": 1, "ticker": "NVDA", "title": "NVIDIA CORP"},
        "1": {"cik_str": 2, "ticker": "AAPL", "title": "Apple Inc."},
    }
    with patch("candidate_sources.requests.get", return_value=FakeResponse(payload)):
        names = fetch_sec_edgar_company_names()
    assert names == ["NVIDIA CORP", "Apple Inc."]


def test_wikipedia_category_pagination_via_cmcontinue():
    page1 = {
        "query": {"categorymembers": [{"title": "Acme Trucking"}]},
        "continue": {"cmcontinue": "next-page-token"},
    }
    page2 = {"query": {"categorymembers": [{"title": "Beta Logistics"}]}}
    responses = iter([FakeResponse(page1), FakeResponse(page2)])

    def fake_get(url, params, headers, timeout):
        return next(responses)

    with patch("candidate_sources.requests.get", side_effect=fake_get):
        names = fetch_wikipedia_category_companies(["Trucking companies of the United States"])

    assert names == ["Acme Trucking", "Beta Logistics"]


def test_wikipedia_list_of_meta_articles_are_filtered_out():
    # Regression: "List of biotech and pharmaceutical companies in the
    # New York metropolitan area" is a real member of Category:
    # Pharmaceutical companies -- an index article, not a company --
    # confirmed live to have slugified into a domain guess long enough to
    # crash discovery.py's whole loop (see discovery.py's _probe_jsonld
    # note). cmnamespace=0 doesn't exclude it since it's a real article,
    # just not a company one.
    payload = {
        "query": {
            "categorymembers": [
                {"title": "Acme Pharma"},
                {"title": "List of biotech and pharmaceutical companies in the New York metropolitan area"},
                {"title": "list of things (lowercase variant)"},
            ]
        }
    }
    with patch("candidate_sources.requests.get", return_value=FakeResponse(payload)):
        names = fetch_wikipedia_category_companies(["Pharmaceutical companies"])
    assert names == ["Acme Pharma"]


def test_wikipedia_multiple_categories_are_all_fetched():
    def fake_get(url, params, headers, timeout):
        cat = params["cmtitle"]
        return FakeResponse({"query": {"categorymembers": [{"title": f"Company in {cat}"}]}})

    with patch("candidate_sources.requests.get", side_effect=fake_get):
        names = fetch_wikipedia_category_companies(["Category A", "Category B"])

    assert len(names) == 2
    assert any("Category A" in n for n in names)
    assert any("Category B" in n for n in names)


def test_latest_commoncrawl_index_picks_first_entry():
    payload = [{"id": "CC-MAIN-2026-30"}, {"id": "CC-MAIN-2026-25"}]
    with patch("candidate_sources.requests.get", return_value=FakeResponse(payload)):
        assert _latest_commoncrawl_index() == "CC-MAIN-2026-30"


def test_greenhouse_tokens_deduped_lowercased_across_both_domains_and_robots_excluded():
    # Confirmed live: boards.greenhouse.io and job-boards.greenhouse.io
    # (Greenhouse's older and newer canonical domains) both have real,
    # current coverage, and every page includes some robots.txt hits
    # mixed in with real job-board pages.
    def fake_get(url, params=None, headers=None, timeout=None):
        params = params or {}
        if params.get("showNumPages"):
            return FakeResponse({"pages": 1})
        if "boards.greenhouse.io" in params["url"] and "job-boards" not in params["url"]:
            return FakeResponse(text=_cdx_jsonl(
                "https://boards.greenhouse.io/Acme/jobs/123",
                "https://boards.greenhouse.io/ACME/jobs/456",  # same company, different casing
                "https://boards.greenhouse.io/robots.txt",
            ))
        return FakeResponse(text=_cdx_jsonl(
            "https://job-boards.greenhouse.io/Beta/jobs/789",
        ))

    with patch("candidate_sources.requests.get", side_effect=fake_get):
        tokens = fetch_commoncrawl_greenhouse_tokens(index="CC-MAIN-TEST")

    assert tokens == ["acme", "beta"]


def test_workday_tenants_skip_locale_segments_and_robots_and_pick_most_common_site():
    # Confirmed live: a real Workday board has many URLs per tenant --
    # locale-prefixed pages (en-US, de-DE, ...), a robots.txt hit, and
    # more than one real site-name variant across different crawled
    # pages. The most CONFIRMED-LIVE-frequent one is picked, not
    # necessarily the "canonical" one (there's no way to know which is
    # canonical from CDX data alone).
    urls = [
        "https://acme.wd1.myworkdayjobs.com/robots.txt",
        "https://acme.wd1.myworkdayjobs.com/en-US/Search/job/abc",
        "https://acme.wd1.myworkdayjobs.com/de-DE/Search",
        "https://acme.wd1.myworkdayjobs.com/Search/job/xyz",
        "https://acme.wd1.myworkdayjobs.com/Search/job/def",
        "https://acme.wd1.myworkdayjobs.com/External_Careers/job/ghi",
    ]

    def fake_get(url, params=None, headers=None, timeout=None):
        params = params or {}
        if params.get("showNumPages"):
            return FakeResponse({"pages": 1})
        return FakeResponse(text=_cdx_jsonl(*urls))

    with patch("candidate_sources.requests.get", side_effect=fake_get):
        results = fetch_commoncrawl_workday_tenants(index="CC-MAIN-TEST")

    assert results == [{"tenant": "acme", "wd_host": "wd1", "site": "Search"}]


def test_workable_tokens_are_lowercased_path_tokens():
    def fake_get(url, params=None, headers=None, timeout=None):
        params = params or {}
        if params.get("showNumPages"):
            return FakeResponse({"pages": 1})
        return FakeResponse(text=_cdx_jsonl(
            "https://apply.workable.com/Back-Market/j/ABC123/",
            "https://apply.workable.com/back-market/j/DEF456/",
            "https://apply.workable.com/robots.txt",
        ))

    with patch("candidate_sources.requests.get", side_effect=fake_get):
        tokens = fetch_commoncrawl_workable_tokens(index="CC-MAIN-TEST")

    assert tokens == ["back-market"]


def test_icims_slugs_require_careers_prefix_and_ignore_other_subdomains():
    # Confirmed live: *.icims.com hosts plenty of non-board subdomains
    # (cdn, images, www) that this pattern's broad CDX query also
    # returns -- only the "careers-" ones are real candidate boards.
    def fake_get(url, params=None, headers=None, timeout=None):
        params = params or {}
        if params.get("showNumPages"):
            return FakeResponse({"pages": 1})
        return FakeResponse(text=_cdx_jsonl(
            "https://careers-acme.icims.com/jobs/search?ss=1",
            "https://CAREERS-ACME.icims.com/jobs/5512/intern/job",
            "https://cdn02.icims.com/a/images.icims.com/script.js",
            "https://www.icims.com/",
        ))

    with patch("candidate_sources.requests.get", side_effect=fake_get):
        slugs = fetch_commoncrawl_icims_slugs(index="CC-MAIN-TEST")

    assert slugs == ["acme"]


def test_bamboohr_tokens_from_careers_subdomain():
    def fake_get(url, params=None, headers=None, timeout=None):
        params = params or {}
        if params.get("showNumPages"):
            return FakeResponse({"pages": 1})
        return FakeResponse(text=_cdx_jsonl(
            "https://acme.bamboohr.com/careers/123",
            "https://ACME.bamboohr.com/careers/list",
        ))

    with patch("candidate_sources.requests.get", side_effect=fake_get):
        tokens = fetch_commoncrawl_bamboohr_tokens(index="CC-MAIN-TEST")

    assert tokens == ["acme"]


def test_taleo_tenants_pick_most_common_section_per_tenant():
    urls = [
        "https://wipo.taleo.net/careersection/wp_internship/jobsearch.ftl?lang=en",
        "https://wipo.taleo.net/careersection/wp_internship/jobsearch.ftl?lang=fr",
        "https://wipo.taleo.net/careersection/2/jobsearch.ftl?lang=en",
        "https://nato.taleo.net/careersection/2/jobsearch.ftl?lang=en",
    ]

    def fake_get(url, params=None, headers=None, timeout=None):
        params = params or {}
        if params.get("showNumPages"):
            return FakeResponse({"pages": 1})
        return FakeResponse(text=_cdx_jsonl(*urls))

    with patch("candidate_sources.requests.get", side_effect=fake_get):
        results = fetch_commoncrawl_taleo_tenants(index="CC-MAIN-TEST")

    assert {"tenant": "wipo", "section": "wp_internship"} in results
    assert {"tenant": "nato", "section": "2"} in results
    assert len(results) == 2


def test_commoncrawl_paginates_across_multiple_cdx_pages():
    def fake_get(url, params=None, headers=None, timeout=None):
        params = params or {}
        if params.get("showNumPages"):
            return FakeResponse({"pages": 2})
        page = params["page"]
        token = "first" if page == 0 else "second"
        return FakeResponse(text=_cdx_jsonl(f"https://boards.greenhouse.io/{token}/jobs/1"))

    with patch("candidate_sources.requests.get", side_effect=fake_get):
        tokens = fetch_commoncrawl_greenhouse_tokens(index="CC-MAIN-TEST")

    assert tokens == ["first", "second"]
