
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "service"))

import db
from standardize import standardize_posting
from dedup import compute_company_key, compute_dedup_key

ALL_POSTINGS_FILE = ROOT / "data" / "all_postings.json"
DESC_DIR = ROOT / "dist" / "data" / "descriptions"

def parse_iso_ts(val):
    if not val or not isinstance(val, str):
        return None
    try:
        if val.endswith("Z"):
            val = val[:-1] + "+00:00"
        return datetime.fromisoformat(val)
    except Exception:
        return None

def main():
    seen_ids = set()
    all_raw = []

    # 1. From all_postings.json
    if ALL_POSTINGS_FILE.exists():
        print(f"Reading from {ALL_POSTINGS_FILE}...")
        with open(ALL_POSTINGS_FILE, "r", encoding="utf-8") as f:
            for item in json.load(f):
                pid = item.get("id")
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    all_raw.append(item)

    # 2. From description files
    if DESC_DIR.exists():
        print(f"Reading from description shards in {DESC_DIR}...")
        for f in DESC_DIR.glob("*.json"):
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    d = json.load(fp)
                    pid = d.get("id")
                    if pid and pid not in seen_ids:
                        seen_ids.add(pid)
                        all_raw.append({
                            "id": pid,
                            "company": d.get("company"),
                            "title": d.get("title"),
                            "location": d.get("location"),
                            "url": d.get("url"),
                            "ats": d.get("ats"),
                            "category": d.get("category", ""),
                            "job_function": d.get("job_function", ""),
                            "work_arrangement": d.get("work_arrangement", ""),
                            "cycle_season": d.get("cycle_season", ""),
                            "cycle_year": d.get("cycle_year"),
                            "posted_at": d.get("posted_at"),
                            "posted_at_ts": d.get("posted_at_ts") or d.get("posted_at"),
                            "description": d.get("description", ""),
                            "description_snippet": d.get("description", "")[:280] if d.get("description") else "",
                            "company_key": d.get("company_key"),
                            "dedup_key": d.get("dedup_key"),
                            "first_seen": d.get("first_seen") or d.get("posted_at"),
                            "status": "open"
                        })
            except Exception:
                pass

    print(f"Total unique postings assembled: {len(all_raw)}")

    # Standardize all postings
    standardized_list = [standardize_posting(p) for p in all_raw]

    # Save to data/all_postings.json
    print(f"Saving {len(standardized_list)} postings to {ALL_POSTINGS_FILE}...")
    with open(ALL_POSTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(standardized_list, f, indent=2, ensure_ascii=False)

    # Seed into Postgres
    print(f"Seeding {len(standardized_list)} postings into Postgres...")
    inserted = 0
    with db.cursor() as cur:
        for std_p in standardized_list:
            pid = std_p["id"]
            company = std_p["company"]
            title = std_p["title"]
            location = std_p["location"]
            url = std_p.get("url", "")
            ats = std_p.get("ats", "")
            category = std_p.get("category", "")
            job_function = std_p.get("job_function", "")
            work_arrangement = std_p.get("work_arrangement", "")
            cycle_season = std_p.get("cycle_season", "")
            cycle_year = std_p.get("cycle_year")
            posted_at = std_p.get("posted_at")
            raw_ts = std_p.get("posted_at_ts")
            posted_at_ts = parse_iso_ts(raw_ts)
            posted_at_approx = std_p.get("posted_at_approx", False)
            snippet = std_p.get("description_snippet", "")
            desc = std_p.get("description", "")
            source_entry = std_p.get("source_entry") or company
            first_seen = parse_iso_ts(std_p.get("first_seen")) or datetime.now(timezone.utc)
            last_seen = parse_iso_ts(std_p.get("last_seen")) or datetime.now(timezone.utc)
            
            dedup_key = compute_dedup_key(company, title, location)
            company_key = compute_company_key(company)
            
            cur.execute("""
                INSERT INTO postings (
                    id, source_entry, company, title, location, url, ats,
                    category, job_function, work_arrangement, cycle_season, cycle_year,
                    posted_at, posted_at_ts, posted_at_approx, description_snippet,
                    description, status, dedup_key, company_key, first_seen, last_seen
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, 'open', %s, %s, %s, %s
                )
                ON CONFLICT (id) DO UPDATE SET
                    company = EXCLUDED.company,
                    title = EXCLUDED.title,
                    location = EXCLUDED.location,
                    category = EXCLUDED.category,
                    job_function = EXCLUDED.job_function,
                    work_arrangement = EXCLUDED.work_arrangement,
                    cycle_season = EXCLUDED.cycle_season,
                    cycle_year = EXCLUDED.cycle_year,
                    company_key = EXCLUDED.company_key,
                    dedup_key = EXCLUDED.dedup_key,
                    last_seen = EXCLUDED.last_seen
            """, (
                pid, source_entry, company, title, location, url, ats,
                category, job_function, work_arrangement, cycle_season, cycle_year,
                posted_at, posted_at_ts, posted_at_approx, snippet,
                desc, dedup_key, company_key, first_seen, last_seen
            ))
            inserted += 1

    print(f"Successfully inserted/updated {inserted} postings in Postgres.")

if __name__ == "__main__":
    main()
