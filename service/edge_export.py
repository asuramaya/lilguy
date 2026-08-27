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
  dist/sitemap.xml              -- Sitemap (home page only; see the SPA note in export_edge_bundle)
  dist/robots.txt               -- Crawler policy (copied from service/static)
  dist/llms.txt                 -- AI/LLM orientation to the site and data (copied from service/static)
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
from standardize import standardize_posting, standardize_company_name

STATIC_DIR = ROOT / "service" / "static"
PRESETS_DIR = ROOT / "presets"
DEFAULT_FILTERS_FILE = ROOT / "filters.yaml"


def safe_filename(posting_id: str) -> str:
    """Encode posting ID into a filesystem-safe and URL-safe filename stem."""
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
    """Fetch all open postings with metadata for client-side search, applying standardization."""
    raw_postings = []
    try:
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
            raw_postings = [dict(r) for r in rows]
    except Exception as e:
        print(f"    [Postgres notice] Could not query database ({e}). Checking local files...")

    if len(raw_postings) < 100:
        all_file = ROOT / "data" / "all_postings.json"
        if all_file.exists():
            print(f"    [Postgres dev/test mode detected ({len(raw_postings)} rows)] Loading full dataset from {all_file}...")
            with open(all_file, "r", encoding="utf-8") as f:
                raw_postings = json.load(f)

    postings = []
    for r in raw_postings:
        d = standardize_posting(r)
        # Convert datetimes to ISO strings and ensure posted_at_ts fallback
        for k in ("posted_at_ts", "first_seen", "last_seen", "closed_at"):
            if isinstance(d.get(k), datetime):
                d[k] = d[k].isoformat()
        if not d.get("posted_at_ts"):
            d["posted_at_ts"] = d.get("posted_at") or d.get("first_seen")
        # Strip full description from index feed to keep bundle fast & lightweight
        d.pop("description", None)
        if d.get("description_snippet") and len(d["description_snippet"]) > 280:
            d["description_snippet"] = d["description_snippet"][:280] + "…"
        # Add description hash link for on-demand fetch
        d["desc_id"] = safe_filename(d["id"])
        postings.append(d)
    return postings


def fetch_descriptions(posting_ids: list[str]) -> dict[str, dict]:
    """Fetch full descriptions and related context for postings."""
    desc_map = {}
    try:
        with cursor() as cur:
            cur.execute("""
                SELECT id, company, title, location, url, ats, posted_at,
                       description, company_key, dedup_key
                FROM postings
                WHERE status = 'open' AND description IS NOT NULL AND description <> ''
            """)
            rows = cur.fetchall()
            for r in rows:
                d = dict(r)
                std = standardize_posting(d)
                desc_map[r["id"]] = {
                    "id": r["id"],
                    "company": std["company"],
                    "company_key": std["company_key"],
                    "title": std["title"],
                    "location": std["location"],
                    "url": r["url"],
                    "ats": std["ats"],
                    "category": std["category"],
                    "job_function": std["job_function"],
                    "work_arrangement": std["work_arrangement"],
                    "cycle_season": std["cycle_season"],
                    "cycle_year": std["cycle_year"],
                    "posted_at": r["posted_at"],
                    "description": r["description"],
                    "dedup_key": r.get("dedup_key")
                }
    except Exception:
        pass

    # Ingest from data/all_postings.json if available
    all_file = ROOT / "data" / "all_postings.json"
    if all_file.exists():
        try:
            with open(all_file, "r", encoding="utf-8") as f:
                all_raw = json.load(f)
                for r in all_raw:
                    pid = r.get("id")
                    desc = r.get("description")
                    if pid and desc and pid not in desc_map:
                        std = standardize_posting(r)
                        desc_map[pid] = {
                            "id": pid,
                            "company": std["company"],
                            "company_key": std["company_key"],
                            "title": std["title"],
                            "location": std["location"],
                            "url": r.get("url", ""),
                            "ats": std["ats"],
                            "category": std["category"],
                            "job_function": std["job_function"],
                            "work_arrangement": std["work_arrangement"],
                            "cycle_season": std["cycle_season"],
                            "cycle_year": std["cycle_year"],
                            "posted_at": r.get("posted_at", ""),
                            "description": desc,
                            "dedup_key": r.get("dedup_key")
                        }
        except Exception:
            pass

    # Merge with existing files in dist/data/descriptions if DB didn't have all descriptions
    existing_desc_dir = ROOT / "dist" / "data" / "descriptions"
    if existing_desc_dir.exists():
        for f in existing_desc_dir.glob("*.json"):
            try:
                with open(f, "r", encoding="utf-8") as df:
                    data = json.load(df)
                    pid = data.get("id")
                    if pid and pid not in desc_map:
                        std = standardize_posting(data)
                        data["company"] = std["company"]
                        data["company_key"] = std["company_key"]
                        data["ats"] = std["ats"]
                        data["category"] = std["category"]
                        data["job_function"] = std["job_function"]
                        data["work_arrangement"] = std["work_arrangement"]
                        desc_map[pid] = data
            except Exception:
                pass
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
    locations = {}

    for p in postings:
        c = p.get("company")
        if c:
            companies[c] = companies.get(c, 0) + 1

        cat = p.get("category")
        if cat and cat != "Uncategorized":
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

        loc = p.get("location")
        if loc and loc != "Not Specified":
            locations[loc] = locations.get(loc, 0) + 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_open_postings": len(postings),
        "total_companies": len(companies),
        "categories": dict(sorted(categories.items(), key=lambda x: x[1], reverse=True)),
        "job_functions": dict(sorted(job_functions.items(), key=lambda x: x[1], reverse=True)),
        "work_arrangements": dict(sorted(work_arrangements.items(), key=lambda x: x[1], reverse=True)),
        "cycle_seasons": dict(sorted(cycle_seasons.items(), key=lambda x: x[1], reverse=True)),
        "cycle_years": dict(sorted(cycle_years.items(), key=lambda x: x[1], reverse=True)),
        "ats_breakdown": dict(sorted(ats_breakdown.items(), key=lambda x: x[1], reverse=True)),
        "top_locations": dict(sorted(locations.items(), key=lambda x: x[1], reverse=True)[:30])
    }


def build_companies_directory(postings: list[dict]) -> list[dict]:
    """Build company index with posting counts and ATS platforms."""
    company_map = {}
    for p in postings:
        c_name = p.get("company", "").strip()
        ck = p.get("company_key") or c_name.lower()
        if not c_name or not ck:
            continue
        if ck not in company_map:
            company_map[ck] = {
                "key": ck,
                "name": c_name,
                "postings_count": 0,
                "ats_platforms": set(),
                "categories": set(),
                "locations": set()
            }
        comp = company_map[ck]
        comp["postings_count"] += 1
        if p.get("ats"):
            comp["ats_platforms"].add(p["ats"])
        if p.get("category") and p.get("category") != "Uncategorized":
            comp["categories"].add(p["category"])
        if p.get("location") and p.get("location") != "Not Specified":
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
    """Main export routine."""
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

    data_dir.mkdir(parents=True, exist_ok=True)
    if include_descriptions:
        desc_dir.mkdir(parents=True, exist_ok=True)

    print("==> Fetching & standardizing open postings...")
    postings = fetch_open_postings()
    print(f"    Processed {len(postings)} open postings.")

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
                desc_file = desc_dir / f"{p['desc_id']}.json"
                with open(desc_file, "w", encoding="utf-8") as f:
                    json.dump(desc_map[pid], f, ensure_ascii=False)
                written_desc += 1
        print(f"    Wrote/updated {written_desc} description JSON files in {desc_dir}")

    # 6. Generate Atom Feed
    atom_file = out_dir / "feed.atom"
    try:
        atom_xml = render_atom(
            postings[:50],
            title="lilguy · Internships & Early Career Roles",
            self_url="https://lilguy.win/feed.atom",
            feed_slug="global"
        )
        with open(atom_file, "w", encoding="utf-8") as f:
            f.write(atom_xml)
        print(f"    Wrote {atom_file}")
    except Exception as e:
        print(f"    [Warning] Atom generation failed: {e}")

    # 7. sitemap.xml -- regenerated each export so lastmod tracks the
    # actual export time. Only the home page is listed: this is a
    # client-rendered SPA (posting/company "pages" are just ?query
    # params on the same document), so per-posting URLs would all
    # resolve to identical unhydrated HTML and go stale the moment a
    # posting closes -- no crawler value, just noise.
    sitemap_file = out_dir / "sitemap.xml"
    lastmod = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(sitemap_file, "w", encoding="utf-8") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://lilguy.win/</loc>
    <lastmod>{lastmod}</lastmod>
    <changefreq>hourly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>
""")
    print(f"    Wrote {sitemap_file}")

    # 8. Cloudflare headers & redirects
    headers_file = out_dir / "_headers"
    with open(headers_file, "w", encoding="utf-8") as f:
        f.write("""# Cache rules for static assets and data shards
/index.html
  Cache-Control: public, max-age=0, must-revalidate
/
  Cache-Control: public, max-age=0, must-revalidate
/data/feed.json
  Cache-Control: public, max-age=60, s-maxage=60
/data/meta.json
  Cache-Control: public, max-age=60, s-maxage=60
/data/presets.json
  Cache-Control: public, max-age=3600, s-maxage=3600
/data/companies.json
  Cache-Control: public, max-age=3600, s-maxage=3600
/data/descriptions/*
  Cache-Control: public, max-age=86400, s-maxage=86400
/sitemap.xml
  Cache-Control: public, max-age=3600, s-maxage=3600
/robots.txt
  Cache-Control: public, max-age=3600, s-maxage=3600
/llms.txt
  Cache-Control: public, max-age=3600, s-maxage=3600

# Security headers
/*
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: camera=(), microphone=(), geolocation=()
""")

    redirects_file = out_dir / "_redirects"
    with open(redirects_file, "w", encoding="utf-8") as f:
        f.write("""# SPA fallback routing
/p/*  /index.html  200
/c/*  /index.html  200
/health  /data/meta.json  200
/api/feed  /data/feed.json  200
/api/meta  /data/meta.json  200
/api/presets  /data/presets.json  200
/api/companies  /data/companies.json  200

# Catch-all: any other unmatched path (stray/legacy links, direct
# navigation) resolves to the SPA shell instead of Cloudflare's bare
# 404 -- this must stay LAST, first match wins in _redirects.
/*  /index.html  200
""")

    print(f"\n==> Edge export complete! Bundle ready at {out_dir}")


def main():
    parser = argparse.ArgumentParser(description="Export Postgres data to static edge CDN bundle")
    parser.add_argument("--out-dir", default=str(ROOT / "dist"), help="Output directory (default: dist)")
    parser.add_argument("--skip-descriptions", action="store_true", help="Skip individual description shards")
    args = parser.parse_args()

    export_edge_bundle(Path(args.out_dir), include_descriptions=not args.skip_descriptions)


if __name__ == "__main__":
    main()
