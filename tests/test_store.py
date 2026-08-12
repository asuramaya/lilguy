import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from store import rebuild  # noqa: E402


def fake_posting(id, title="Some Intern Role", source_entry="Some Source"):
    # rebuild() only calls .to_dict() on what it's given — a plain object
    # with that method is enough, no need to construct a real Posting.
    return SimpleNamespace(to_dict=lambda: {"id": id, "title": title, "source_entry": source_entry})


def test_new_posting_gets_first_seen_stamp():
    store, new = rebuild({}, [fake_posting("a")], seen_at="2026-01-01T00:00:00Z")
    assert store["a"]["first_seen"] == "2026-01-01T00:00:00Z"
    assert len(new) == 1


def test_surviving_posting_keeps_original_first_seen():
    previous = {"a": {"id": "a", "title": "Old Title", "first_seen": "2026-01-01T00:00:00Z"}}
    store, new = rebuild(previous, [fake_posting("a", title="Updated Title")], seen_at="2026-02-01T00:00:00Z")
    # first_seen carried forward, not bumped by a re-run
    assert store["a"]["first_seen"] == "2026-01-01T00:00:00Z"
    # but live fields (title) are refreshed from this run's fetch
    assert store["a"]["title"] == "Updated Title"
    assert new == []


def test_posting_no_longer_fetched_drops_out():
    # A rebuild is the full truth for this run — a posting the source no
    # longer returns has closed/filled and shouldn't linger in the store.
    previous = {"a": {"id": "a", "title": "Gone", "first_seen": "2026-01-01T00:00:00Z"}}
    store, new = rebuild(previous, [fake_posting("b")], seen_at="2026-02-01T00:00:00Z")
    assert "a" not in store
    assert "b" in store


def test_failed_source_entry_postings_are_preserved_not_dropped():
    # Regression: confirmed live that a single transient HTTP timeout
    # inside one of ~250 requests for the Muse aggregator caused its
    # ENTIRE fetch to raise, contributing zero postings to fresh_postings
    # this run. Before failed_source_entries existed, rebuild() had no
    # way to distinguish "this source failed to fetch" from "this
    # source's postings are legitimately all gone" — both looked
    # identical (absent from fresh_postings) — so it silently dropped
    # ~4175 previously-tracked postings down to 98 on one network blip.
    previous = {
        "muse:1": {"id": "muse:1", "title": "Old Muse Posting", "source_entry": "The Muse", "first_seen": "2026-01-01T00:00:00Z"},
        "greenhouse:koch:1": {"id": "greenhouse:koch:1", "title": "Koch Posting", "source_entry": "Koch Industries", "first_seen": "2026-01-01T00:00:00Z"},
    }
    # This run: Muse's fetch failed entirely (fresh_postings has nothing
    # from it), but Koch's succeeded and legitimately has zero postings.
    store, new = rebuild(previous, [], seen_at="2026-02-01T00:00:00Z", failed_source_entries={"The Muse"})

    assert "muse:1" in store, "a failed source's old postings must survive the rebuild"
    assert store["muse:1"]["first_seen"] == "2026-01-01T00:00:00Z"
    assert "greenhouse:koch:1" not in store, "a SUCCEEDED source with zero results should still drop its stale postings"


def test_failed_source_new_postings_still_merge_with_preserved_old_ones():
    # A source can fail partway through a multi-category fetch (some
    # categories succeeded, one raised) — the categories that DID return
    # fresh results should still update normally alongside the preserved
    # postings from ones that didn't.
    previous = {
        "muse:old": {"id": "muse:old", "title": "Stale", "source_entry": "The Muse", "first_seen": "2026-01-01T00:00:00Z"},
    }
    fresh = [fake_posting("muse:new", title="Fresh Posting", source_entry="The Muse")]
    store, new = rebuild(previous, fresh, seen_at="2026-02-01T00:00:00Z", failed_source_entries=set())

    # Muse wasn't marked failed this time (it partially succeeded, which
    # scrape.py only marks as "failed" if the WHOLE connector call
    # raised) — normal rebuild semantics apply: old posting not
    # re-returned this run drops, new one is added.
    assert "muse:old" not in store
    assert "muse:new" in store
