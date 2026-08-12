#!/usr/bin/env python3
"""Fetch every internship-shaped posting from every source in sources.yaml
and update data/all_postings.json — the complete, DOMAIN-UNFILTERED raw
feed. Then apply this fork's default filters.yaml on top to produce
FEED.md, the same way anyone else can apply their own filter file with
build_feed.py without re-scraping.

Run manually (`python scraper/scrape.py`) or on a schedule via
.github/workflows/scrape.yml.

Exits non-zero on a source that errors, AFTER processing every other
source — one broken/renamed token shouldn't hide postings from the rest.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from build_feed import DEFAULT_FILTERS_FILE, FEED_FILE, build_and_write  # noqa: E402
from connectors import CONNECTORS  # noqa: E402
from filters import is_internship  # noqa: E402
from store import load_opportunities, rebuild, save_opportunities  # noqa: E402

ROOT = Path(__file__).parent.parent
SOURCES_FILE = ROOT / "sources.yaml"
ALL_POSTINGS_FILE = ROOT / "data" / "all_postings.json"


def load_sources() -> list[dict]:
    with SOURCES_FILE.open() as f:
        config = yaml.safe_load(f) or {}
    return config.get("sources", [])


def fetch_all(sources: list[dict]) -> tuple[list, list[str], set]:
    all_postings = []
    errors = []
    failed_entries = set()
    for entry in sources:
        label = entry.get("company", "?")
        ats = entry.get("ats")
        connector_cls = CONNECTORS.get(ats)
        if connector_cls is None:
            errors.append(f"{label}: unknown ats '{ats}' (known: {list(CONNECTORS)})")
            failed_entries.add(label)
            continue
        try:
            postings = connector_cls().fetch(entry)
        except Exception as exc:  # noqa: BLE001 - one bad source must not kill the run
            errors.append(f"{label} ({ats}): {exc}")
            failed_entries.add(label)
            continue
        for p in postings:
            p.source_entry = label
        relevant = [p for p in postings if is_internship(p.title)]
        all_postings.extend(relevant)
        print(f"  {label:35s} {len(postings):5d} fetched -> {len(relevant):4d} internship-shaped")
    return all_postings, errors, failed_entries


def main() -> int:
    sources = load_sources()
    print(f"Fetching {len(sources)} source(s)...")
    fresh_postings, errors, failed_entries = fetch_all(sources)

    previous = load_opportunities(ALL_POSTINGS_FILE)
    seen_at = datetime.now(timezone.utc).isoformat()
    new_store, newly_added = rebuild(previous, fresh_postings, seen_at, failed_entries)
    save_opportunities(ALL_POSTINGS_FILE, new_store)

    preserved = sum(1 for d in new_store.values() if d.get("source_entry") in failed_entries)
    if preserved:
        print(f"\n{preserved} posting(s) from {len(failed_entries)} failed source(s) carried forward unchanged (not treated as closed).")
    print(f"{len(new_store)} open posting(s) in the raw store, {len(newly_added)} new this run.")

    # This fork's own default view, built from the same raw store anyone
    # else's build_feed.py --filters my-filters.yaml would read.
    filtered_count = build_and_write(ALL_POSTINGS_FILE, DEFAULT_FILTERS_FILE, FEED_FILE, "scraper/scrape.py")
    print(f"{filtered_count} posting(s) in FEED.md after this fork's default filter ({DEFAULT_FILTERS_FILE.name}).")

    for p in newly_added:
        print(f"  NEW (raw): {p['company']} — {p['title']} ({p['url']})")

    if errors:
        print(f"\n{len(errors)} source(s) failed:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
