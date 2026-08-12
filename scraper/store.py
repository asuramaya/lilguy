import json
from pathlib import Path


def load_opportunities(path: Path) -> dict:
    """Returns {posting_id: posting_dict}. Empty dict if the file doesn't exist yet."""
    if not path.exists():
        return {}
    with path.open() as f:
        data = json.load(f)
    return {p["id"]: p for p in data}


def save_opportunities(path: Path, by_id: dict) -> None:
    ordered = sorted(by_id.values(), key=lambda p: p.get("first_seen", ""), reverse=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(ordered, f, indent=2)
        f.write("\n")


def rebuild(previous: dict, fresh_postings: list, seen_at: str, failed_source_entries: set = frozenset()) -> tuple[dict, list]:
    """Rebuild the store from this run's full fetch across all sources.

    A run's `fresh_postings` is every currently-open posting from every
    SUCCESSFULLY FETCHED source this run — for those sources, that IS the
    current truth, so a posting no longer returned has closed/filled and
    drops out.

    `failed_source_entries` is the set of sources.yaml entry labels (the
    `company:` field) whose fetch() call raised this run. Their postings
    from `previous` are carried forward UNCHANGED rather than treated as
    closed — a source failing to fetch is not the same fact as a source
    reporting its postings gone, and conflating the two is a real bug
    this project hit live: one transient HTTP timeout inside a single
    request out of ~250 for the Muse aggregator caused its ENTIRE fetch
    to raise, and because the store previously had no way to distinguish
    "Muse fetch failed" from "Muse now has zero postings," the rebuild
    silently dropped ~4175 tracked postings down to 98 on one bad network
    blip. Scoped by `source_entry` (which sources.yaml entry produced a
    posting), not by `source` (the ats type) — several entries can share
    an ats type, and only the specific failed entry's postings should be
    preserved, not every posting that happens to share its connector.

    `previous` is also consulted, for surviving postings, to carry
    forward each one's original first_seen so re-running the scraper
    doesn't repeatedly bump its "posted" date.

    Returns (new_store, newly_added_postings) — the latter is what should
    actually be announced (new commit, notification, etc.) this run.
    """
    new_store = {}
    newly_added = []

    if failed_source_entries:
        for posting_id, d in previous.items():
            if d.get("source_entry") in failed_source_entries:
                new_store[posting_id] = d

    for posting in fresh_postings:
        d = posting.to_dict()
        prior = previous.get(d["id"])
        if prior:
            d["first_seen"] = prior.get("first_seen", seen_at)
        else:
            d["first_seen"] = seen_at
            newly_added.append(d)
        new_store[d["id"]] = d

    return new_store, newly_added
