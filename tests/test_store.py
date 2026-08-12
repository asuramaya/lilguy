import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from store import rebuild  # noqa: E402


def fake_posting(id, title="Some Intern Role"):
    # rebuild() only calls .to_dict() on what it's given — a plain object
    # with that method is enough, no need to construct a real Posting.
    return SimpleNamespace(to_dict=lambda: {"id": id, "title": title})


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
