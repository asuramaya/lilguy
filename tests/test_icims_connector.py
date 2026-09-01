import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))
from connectors.icims import IcimsConnector

# Shape confirmed live against a real board (careers-federatedinsurance.icims.com).
PAGE_1_HTML = """
<div class="row"><h2 class="iCIMS_SubHeader iCIMS_SubHeader_Jobs">
Search Results
Page 1 of 2
</h2></div>
<ul class="container-fluid iCIMS_JobsTable">
<li class="iCIMS_JobCardItem">
<div class="row">
<div class="col-xs-6 header left">
<span class="sr-only field-label">Job Locations</span>
<span >
US-GA-Atlanta</span>
</div>
<div class="col-xs-6 header right">
<span class="sr-only field-label">Job ID</span>
<span >
2026-5512</span>
</div>
<div class="col-xs-12 title">
<a href="https://careers-acme.icims.com/jobs/5512/2027-intern/job?in_iframe=1" class="iCIMS_Anchor" title="5512 - Intern">
<span class="sr-only field-label">Job Title</span>
<h3 >
2027 Claims College Internship - Atlanta, GA</h3>
</a>
</div>
<div class="col-xs-12 description">
Great internship opportunity in Atlanta.</div>
</div>
</li>
</ul>
"""

PAGE_2_HTML = PAGE_1_HTML.replace("5512", "5513").replace("Atlanta", "Boston").replace("GA", "MA")


def test_icims_missing_slug_raises():
    c = IcimsConnector()
    with pytest.raises(ValueError, match="missing 'slug'"):
        c.fetch({"company": "Acme"})


def test_icims_bad_slug_raises(monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **kw: SimpleNamespace(status_code=404, text="gone"),
    )
    c = IcimsConnector()
    with pytest.raises(ValueError, match="returned HTTP 404"):
        c.fetch({"company": "Acme", "slug": "acme"})


def test_icims_parse_and_paginate(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if "pr=0" in url:
            return SimpleNamespace(status_code=200, text=PAGE_1_HTML)
        return SimpleNamespace(status_code=200, text=PAGE_2_HTML)

    monkeypatch.setattr("requests.get", fake_get)

    c = IcimsConnector()
    postings = c.fetch({"ats": "icims", "company": "Acme Corp", "slug": "acme", "category": "Tech"})

    assert len(calls) == 2  # stopped after page 2 of 2
    assert len(postings) == 2

    p1 = postings[0]
    assert p1.id == "icims:acme:2026-5512"
    assert p1.title == "2027 Claims College Internship - Atlanta, GA"
    assert p1.location == "Atlanta, GA"
    assert "Great internship" in p1.description

    p2 = postings[1]
    assert p2.id == "icims:acme:2026-5513"
    assert p2.location == "Boston, MA"
