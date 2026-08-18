"""Atom output: well-formed, correctly dated, and safely escaped."""
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

from atom import render_atom  # noqa: E402

NS = {"a": "http://www.w3.org/2005/Atom"}


def _posting(**over):
    base = {
        "id": "greenhouse:acme:123",
        "title": "Supply Chain Intern",
        "company": "Acme",
        "location": "Austin, TX",
        "category": "Logistics",
        "url": "https://example.com/jobs/123",
        "posted_at_ts": "2026-07-09T10:58:08+00:00",
        "posted_at_approx": False,
        "first_seen": "2026-08-15T00:00:00+00:00",
    }
    base.update(over)
    return base


def _parse(xml):
    return ElementTree.fromstring(xml)


def test_output_is_well_formed_and_has_one_entry_per_posting():
    xml = render_atom([_posting(), _posting(id="greenhouse:acme:124", title="Ops Intern")],
                       title="T", self_url="https://x/feed.atom", feed_slug="feed/x")
    root = _parse(xml)
    assert len(root.findall("a:entry", NS)) == 2


def test_entry_uses_the_employers_date_not_discovery_time():
    # first_seen is a month later than posted_at_ts here; using it would
    # make an old posting look new to every subscriber's reader.
    xml = render_atom([_posting()], title="T", self_url="https://x", feed_slug="f")
    updated = _parse(xml).find("a:entry/a:updated", NS).text
    assert updated.startswith("2026-07-09")


def test_entry_falls_back_to_first_seen_when_there_is_no_posted_date():
    xml = render_atom([_posting(posted_at_ts=None)], title="T", self_url="https://x", feed_slug="f")
    assert _parse(xml).find("a:entry/a:updated", NS).text.startswith("2026-08-15")


def test_feed_updated_is_the_newest_entry_not_now():
    # If the feed always stamped "now", every poll would look like a
    # change and readers lose the cheap no-op path.
    xml = render_atom(
        [_posting(posted_at_ts="2026-07-09T00:00:00+00:00"),
         _posting(id="b", posted_at_ts="2026-08-01T00:00:00+00:00")],
        title="T", self_url="https://x", feed_slug="f")
    root = _parse(xml)
    feed_updated = root.find("a:updated", NS).text
    assert feed_updated.startswith("2026-08-01")
    assert not feed_updated.startswith(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H"))


def test_approximate_dates_are_disclosed_in_the_summary():
    xml = render_atom([_posting(posted_at_approx=True)], title="T", self_url="https://x", feed_slug="f")
    assert "approximate" in _parse(xml).find("a:entry/a:summary", NS).text


def test_ids_are_stable_and_unique_per_posting():
    xml = render_atom([_posting(), _posting(id="greenhouse:acme:124")],
                       title="T", self_url="https://x", feed_slug="f")
    ids = [e.text for e in _parse(xml).findall("a:entry/a:id", NS)]
    assert len(set(ids)) == 2
    assert all(i.startswith("tag:internship-feed,2026:") for i in ids)


def test_markup_in_provider_data_cannot_break_the_document():
    # Titles and companies come from third-party job boards; an
    # unescaped ampersand or angle bracket would produce a document every
    # reader rejects.
    nasty = 'Intern <script>alert("x")</script> & "friends"'
    xml = render_atom([_posting(title=nasty, company="A & B <Ltd>")],
                       title="T & T", self_url="https://x?a=1&b=2", feed_slug="f")
    root = _parse(xml)  # would raise if malformed
    assert root.find("a:entry/a:title", NS).text == nasty


def test_empty_feed_is_still_valid():
    root = _parse(render_atom([], title="T", self_url="https://x", feed_slug="f"))
    assert root.findall("a:entry", NS) == []
    assert root.find("a:updated", NS) is not None


def test_an_approximate_date_states_that_it_is_a_lower_bound():
    # "Approximate" alone reads as a fuzzy midpoint. Every approximate
    # value in this corpus is a LOWER BOUND on age -- Workday stops
    # counting at "Posted 30+ Days Ago" -- so a subscriber told merely
    # "approximate" would conclude the posting is roughly this fresh,
    # which is the opposite of the truth.
    xml = render_atom(
        [_posting(posted_at_approx=True)],
        title="t", self_url="https://x/feed.atom", feed_slug="feed/x",
    )
    assert "AT LEAST" in xml
    assert "considerably older" in xml


def test_an_exact_date_carries_no_bound_language():
    xml = render_atom(
        [_posting(posted_at_approx=False)],
        title="t", self_url="https://x/feed.atom", feed_slug="feed/x",
    )
    assert "AT LEAST" not in xml
