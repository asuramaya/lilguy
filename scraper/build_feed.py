#!/usr/bin/env python3
"""Build a personalized feed from the raw posting store + a filter spec —
no re-scraping. This is the tool a fork's own reader runs to get their own
view of data/all_postings.json without needing to touch scraper code:

  python scraper/build_feed.py
      -> applies filters.yaml, writes FEED.md (this fork's default view)

  python scraper/build_feed.py --filters my-filters.yaml --out MY_FEED.md
      -> applies your own copy of filters.yaml, writes wherever you want

Copy filters.yaml, edit its keyword/company lists to your own interest
(marketing, software engineering, finance, whatever), point --filters at
your copy. See filters.yaml's own comments for the schema.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from feed_writer import render_feed_markdown  # noqa: E402
from store import load_opportunities  # noqa: E402
from user_filter import apply_filter, load_filter  # noqa: E402

ROOT = Path(__file__).parent.parent
ALL_POSTINGS_FILE = ROOT / "data" / "all_postings.json"
DEFAULT_FILTERS_FILE = ROOT / "filters.yaml"
FEED_FILE = ROOT / "FEED.md"


def build_and_write(postings_file: Path, filters_file: Path, out_file: Path, source_label: str) -> int:
    """Returns the count of postings that passed the filter."""
    by_id = load_opportunities(postings_file)
    if not by_id:
        raise SystemExit(
            f"{postings_file} is empty or missing — run scraper/scrape.py first to populate the raw store."
        )
    spec = load_filter(str(filters_file))
    postings = sorted(by_id.values(), key=lambda p: p.get("first_seen", ""), reverse=True)
    filtered = apply_filter(postings, spec)

    markdown = render_feed_markdown(filtered, newly_added_ids=set(), source_label=source_label, filter_name=spec.get("name", filters_file.stem))
    out_file.write_text(markdown)
    return len(filtered)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--filters", type=Path, default=DEFAULT_FILTERS_FILE, help="Path to a filters.yaml-shaped file")
    parser.add_argument("--out", type=Path, default=FEED_FILE, help="Where to write the rendered feed")
    parser.add_argument("--postings", type=Path, default=ALL_POSTINGS_FILE, help="Path to the raw posting store")
    args = parser.parse_args()

    count = build_and_write(args.postings, args.filters, args.out, "scraper/build_feed.py")
    print(f"{count} posting(s) passed {args.filters} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
