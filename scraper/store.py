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


def rebuild(previous: dict, fresh_postings: list, seen_at: str) -> tuple[dict, list]:
    """Rebuild the store from this run's full fetch across all sources.

    A run's `fresh_postings` is every currently-open posting from every
    configured source — that IS the current truth, so the rebuilt store
    is exactly that set (a posting no longer returned by its ATS has
    closed/filled and drops out). `previous` is consulted only to carry
    forward each surviving posting's original first_seen, so re-running
    the scraper doesn't repeatedly bump its "posted" date.

    Returns (new_store, newly_added_postings) — the latter is what should
    actually be announced (new commit, notification, etc.) this run.
    """
    new_store = {}
    newly_added = []
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
