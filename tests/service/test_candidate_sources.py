import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

from candidate_sources import fetch_sec_edgar_company_names, fetch_wikipedia_category_companies  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


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
