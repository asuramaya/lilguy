#!/usr/bin/env python3
"""Exports postings and edge assets from Postgres into a static bundle
suitable for direct hosting on Cloudflare Pages / CDN (lilguy.win).

Generates:
  dist/data/feed.json           -- Compact array of all active open postings
  dist/data/meta.json           -- Aggregated facets (categories, functions, cycles, counts)
  dist/data/presets.json        -- Preset filter configurations
  dist/data/companies.json      -- Company directory index
  dist/data/descriptions/*.json -- Individual full description files (on-demand)
  dist/feed.atom                -- Global Atom syndication feed
  dist/_headers                 -- Cloudflare Pages HTTP header rules
  dist/_redirects               -- Cloudflare Pages redirect rules
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root and scraper/service to path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scraper"))
sys.path.insert(0, str(ROOT / "service"))

import yaml
from atom import render_atom
from db import cursor

STATIC_DIR = ROOT / "service" / "static"
PRESETS_DIR = ROOT / "presets"
DEFAULT_FILTERS_FILE = ROOT / "filters.yaml"


def safe_filename(posting_id: str) -> str:
    """Encode posting ID into a filesystem-safe and URL-safe filename stem."""
    # Use MD5 hex hash for ultra-safe, collision-free, short filename
    return hashlib.sha256(posting_id.encode("utf-8")).hexdigest()[:20]


def load_all_presets() -> list[dict]:
    """Parse all preset YAML files and default filters.yaml into JSON-ready dicts."""
    presets = []
    
    # 1. Default preset
    if DEFAULT_FILTERS_FILE.exists():
        with open(DEFAULT_FILTERS_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            presets.append({
                "id": "default",
                "name": data.get("name", "Supply Chain / Logistics"),
                "keywords_any": data.get("keywords_any", []),
                "exclude_keywords": data.get("exclude_keywords", []),
                "trusted_companies": data.get("trusted_companies", []),
                "locations_include": data.get("locations_include", []),
                "locations_exclude": data.get("locations_exclude", []),
                "max_age_days": data.get("max_age_days")
            })

    # 2. Other presets
    if PRESETS_DIR.exists():
        for p in sorted(PRESETS_DIR.glob("*.yaml")):
            preset_id = p.stem
            if preset_id in ("operations-logistics-supply-chain", "default"):
                continue
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                presets.append({
                    "id": preset_id,
                    "name": data.get("name", preset_id.replace("-", " ").title()),
                    "keywords_any": data.get("keywords_any", []),
                    "exclude_keywords": data.get("exclude_keywords", []),
                    "trusted_companies": data.get("trusted_companies", []),
                    "locations_include": data.get("locations_include", []),
                    "locations_exclude": data.get("locations_exclude", []),
                    "max_age_days": data.get("max_age_days")
                })

    return presets


def fetch_open_postings() -> list[dict]:
    """Fetch all open postings with metadata for client-side search."""
    with cursor() as cur:
        cur.execute("""
            SELECT id, company, company_key, title, location, url, ats,
                   category, job_function, work_arrangement,
                   cycle_season, cycle_year, posted_at, posted_at_ts,
                   posted_at_approx, first_seen, description_snippet,
                   dedup_key
            FROM postings
            WHERE status = 'open'
            ORDER BY posted_at_ts DESC NULLS LAST, first_seen DESC, id
        """)
        rows = cur.fetchall()

    postings = []
    for r in rows:
        d = dict(r)
        # Convert datetimes to ISO strings
        for k in ("posted_at_ts", "first_seen"):
            if isinstance(d.get(k), datetime):
                d[k] = d[k].isoformat()
        # Add description hash link for on-demand fetch
        d["desc_id"] = safe_filename(d["id"])
        postings.append(d)
    return postings


def fetch_descriptions(posting_ids: list[str]) -> dict[str, dict]:
    """Fetch full descriptions and related context for postings."""
    desc_map = {}
    with cursor() as cur:
        cur.execute("""
            SELECT id, company, title, location, url, ats, posted_at,
                   description, company_key, dedup_key
            FROM postings
            WHERE status = 'open' AND description IS NOT NULL AND description <> ''
        """)
        rows = cur.fetchall()
        for r in rows:
            desc_map[r["id"]] = {
                "id": r["id"],
                "company": r["company"],
                "title": r["title"],
                "location": r["location"],
                "url": r["url"],
                "ats": r["ats"],
                "posted_at": r["posted_at"],
                "description": r["description"],
                "company_key": r["company_key"],
                "dedup_key": r["dedup_key"]
            }
    return desc_map


def build_metadata(postings: list[dict]) -> dict:
    """Compute aggregate facet statistics for the corpus."""
    companies = {}
    categories = {}
    job_functions = {}
    work_arrangements = {}
    cycle_seasons = {}
    cycle_years = {}
    ats_breakdown = {}

    for p in postings:
        c = p.get("company")
        if c:
            companies[c] = companies.get(c, 0) + 1

        cat = p.get("category")
        if cat:
            categories[cat] = categories.get(cat, 0) + 1

        fn = p.get("job_function")
        if fn:
            job_functions[fn] = job_functions.get(fn, 0) + 1

        wa = p.get("work_arrangement")
        if wa:
            work_arrangements[wa] = work_arrangements.get(wa, 0) + 1

        cs = p.get("cycle_season")
        if cs:
            cycle_seasons[cs] = cycle_seasons.get(cs, 0) + 1

        cy = p.get("cycle_year")
        if cy is not None:
            cycle_years[str(cy)] = cycle_years.get(str(cy), 0) + 1

        ats = p.get("ats")
        if ats:
            ats_breakdown[ats] = ats_breakdown.get(ats, 0) + 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_open_postings": len(postings),
        "total_companies": len(companies),
        "categories": dict(sorted(categories.items(), key=lambda x: x[1], reverse=True)),
        "job_functions": dict(sorted(job_functions.items(), key=lambda x: x[1], reverse=True)),
        "work_arrangements": dict(sorted(work_arrangements.items(), key=lambda x: x[1], reverse=True)),
        "cycle_seasons": dict(sorted(cycle_seasons.items(), key=lambda x: x[1], reverse=True)),
        "cycle_years": dict(sorted(cycle_years.items(), key=lambda x: x[1], reverse=True)),
        "ats_breakdown": dict(sorted(ats_breakdown.items(), key=lambda x: x[1], reverse=True))
    }


def build_companies_directory(postings: list[dict]) -> list[dict]:
    """Build company index with posting counts and ATS platforms."""
    company_map = {}
    for p in postings:
        ck = p.get("company_key") or p.get("company", "").lower()
        if not ck:
            continue
        if ck not in company_map:
            company_map[ck] = {
                "key": ck,
                "name": p.get("company"),
                "postings_count": 0,
                "ats_platforms": set(),
                "categories": set(),
                "locations": set()
            }
        comp = company_map[ck]
        comp["postings_count"] += 1
        if p.get("ats"):
            comp["ats_platforms"].add(p["ats"])
        if p.get("category"):
            comp["categories"].add(p["category"])
        if p.get("location"):
            comp["locations"].add(p["location"])

    # Convert sets to sorted lists
    result = []
    for ck, comp in sorted(company_map.items(), key=lambda x: x[1]["postings_count"], reverse=True):
        result.append({
            "key": comp["key"],
            "name": comp["name"],
            "postings_count": comp["postings_count"],
            "ats_platforms": sorted(list(comp["ats_platforms"])),
            "categories": sorted(list(comp["categories"])),
            "locations_sample": sorted(list(comp["locations"]))[:5]
        })
    return result


def export_edge_bundle(out_dir: Path, include_descriptions: bool = True):
    out_dir = Path(out_dir)
    data_dir = out_dir / "data"
    desc_dir = data_dir / "descriptions"

    # Copy static assets (index.html, etc.) into dist
    if STATIC_DIR.exists():
        for item in STATIC_DIR.glob("*"):
            if item.is_file():
                shutil.copy2(item, out_dir / item.name)
            elif item.is_dir() and item.name not in ("__pycache__",):
                shutil.copytree(item, out_dir / item.name, dirs_exist_ok=True)
    """Main export routine."""
    out_dir = Path(out_dir)
    data_dir = out_dir / "data"
    desc_dir = data_dir / "descriptions"

    data_dir.mkdir(parents=True, exist_ok=True)
    if include_descriptions:
        desc_dir.mkdir(parents=True, exist_ok=True)

    print("==> Fetching open postings from Postgres...")
    postings = fetch_open_postings()
    print(f"    Found {len(postings)} open postings.")

    print("==> Computing metadata and facets...")
    meta = build_metadata(postings)

    print("==> Loading preset configurations...")
    presets = load_all_presets()

    print("==> Building companies directory...")
    companies = build_companies_directory(postings)

    # 1. Write feed.json
    feed_file = data_dir / "feed.json"
    with open(feed_file, "w", encoding="utf-8") as f:
        json.dump(postings, f, ensure_ascii=False)
    print(f"    Wrote {feed_file} ({feed_file.stat().st_size / 1024:.1f} KB)")

    # 2. Write meta.json
    meta_file = data_dir / "meta.json"
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print(f"    Wrote {meta_file}")

    # 3. Write presets.json
    presets_file = data_dir / "presets.json"
    with open(presets_file, "w", encoding="utf-8") as f:
        json.dump(presets, f, indent=2, ensure_ascii=False)
    print(f"    Wrote {presets_file}")

    # 4. Write companies.json
    companies_file = data_dir / "companies.json"
    with open(companies_file, "w", encoding="utf-8") as f:
        json.dump(companies, f, indent=2, ensure_ascii=False)
    print(f"    Wrote {companies_file}")

    # 5. Write descriptions
    if include_descriptions:
        print("==> Fetching and writing full description shards...")
        desc_map = fetch_descriptions([p["id"] for p in postings])
        written_desc = 0
        for p in postings:
            pid = p["id"]
            if pid in desc_map:
                fname = desc_dir / f"{safe_filename(pid)}.json"
                with open(fname, "w", encoding="utf-8") as f:
                    json.dump(desc_map[pid], f, ensure_ascii=False)
                written_desc += 1
        print(f"    Wrote {written_desc} description JSON files into {desc_dir}")

    # 6. Render Atom feed
    print("==> Rendering Atom syndication feed...")
    atom_xml = render_atom(
        postings[:500],
        title="Lilguy - Open Internship Feed",
        self_url="https://lilguy.win/feed.atom",
        feed_slug="all"
    )
    atom_file = out_dir / "feed.atom"
    with open(atom_file, "w", encoding="utf-8") as f:
        f.write(atom_xml)
    print(f"    Wrote {atom_file}")

    # 7. Write Cloudflare Pages headers & redirects
    headers_file = out_dir / "_headers"
    headers_content = """# Global Security & Caching Headers
/*
  X-Frame-Options: SAMEORIGIN
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: document-domain=()

# Static Assets
/assets/*
  Cache-Control: public, max-age=31536000, immutable

# API & Data JSON Feeds
/data/*
  Cache-Control: public, max-age=60, s-maxage=300
  Access-Control-Allow-Origin: *
  Access-Control-Allow-Methods: GET, OPTIONS

/feed.atom
  Cache-Control: public, max-age=300, s-maxage=600
  Content-Type: application/atom+xml; charset=utf-8
  Access-Control-Allow-Origin: *
"""
    with open(headers_file, "w", encoding="utf-8") as f:
        f.write(headers_content)
    print(f"    Wrote {headers_file}")


def main():
    parser = argparse.ArgumentParser(description="Export edge data bundle for lilguy.win")
    parser.add_argument("--out-dir", default=str(ROOT / "dist"), help="Output directory for edge bundle")
    parser.add_argument("--skip-descriptions", action="store_true", help="Skip writing individual description files")
    args = parser.parse_args()

    export_edge_bundle(Path(args.out_dir), include_descriptions=not args.skip_descriptions)


if __name__ == "__main__":
    main()
