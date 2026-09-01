import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))
from connectors.taleo import TaleoConnector

JOBSEARCH_HTML = """
<html><script>
require(["...", function() {
    var x = { queryString: 'portal=10105120713' };
}]);
</script></html>
"""

UNAVAILABLE_HTML = "<html><body>Career Section Unavailable</body></html>"

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Acme Careers</title>
<item>
<title>Software Engineering Intern </title>
<link>http://acme.taleo.net/careersection/2/jobdetail.ftl?lang=en&amp;job=1234-INT</link>
<description>Location: Geneva, Switzerland. Join our team as an intern.</description>
<pubDate>Sat, 18 Jul 2026 11:08:47 EDT</pubDate>
</item>
<item>
<title>Data Intern </title>
<link>http://acme.taleo.net/careersection/2/jobdetail.ftl?lang=en&amp;job=5678-INT</link>
<description>No location label here.</description>
<pubDate>Sun, 19 Jul 2026 09:00:00 EDT</pubDate>
</item>
</channel></rss>
"""


def test_taleo_missing_fields_raises():
    c = TaleoConnector()
    with pytest.raises(ValueError, match="needs both"):
        c.fetch({"company": "Acme"})


def test_taleo_unavailable_section_raises(monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **kw: SimpleNamespace(status_code=200, text=UNAVAILABLE_HTML),
    )
    c = TaleoConnector()
    with pytest.raises(ValueError, match="isn't a live career section"):
        c.fetch({"company": "Acme", "tenant": "acme", "section": "2"})


def test_taleo_parse_rss(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        if "joblist.rss" in url:
            assert "portal=10105120713" in url
            return SimpleNamespace(status_code=200, content=RSS_XML.encode(), raise_for_status=lambda: None)
        return SimpleNamespace(status_code=200, text=JOBSEARCH_HTML)

    monkeypatch.setattr("requests.get", fake_get)

    c = TaleoConnector()
    postings = c.fetch({"ats": "taleo", "company": "Acme Corp", "tenant": "acme", "section": "2", "category": "Tech"})
    assert len(postings) == 2

    p1 = postings[0]
    assert p1.id == "taleo:acme:1234-INT"
    assert p1.title == "Software Engineering Intern"
    assert p1.location == "Geneva, Switzerland"

    p2 = postings[1]
    assert p2.id == "taleo:acme:5678-INT"
    assert p2.location == ""
