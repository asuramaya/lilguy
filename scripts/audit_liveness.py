#!/usr/bin/env python3
"""Audit the liveness and validity of job postings in the corpus.

Checks whether postings are still active on their source ATS platforms:
  - Workday: Queries the per-posting CXS API endpoint (/wday/cxs/...). 404/410
    means closed; 403 is treated as UNCERTAIN (blocked), not closed -- measured
    live returning 403 for postings the source's own list fetch still confirms
    open, see service/liveness.py's WORKDAY_GONE_STATUSES comment.
  - Greenhouse, Lever, Ashby, SmartRecruiters: Checks HTTP status and redirect destinations.
  - Generic/Muse/JSON-LD: Inspects HTTP status codes and HTML closure markers.

Usage:
  ./scripts/audit_liveness.py [--limit N] [--workers W] [--json] [--close-dead]
"""

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR / "service"))

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json",
}

GONE_PHRASES = [
    "page doesn't exist",
    "page does not exist",
    "job is no longer available",
    "position is no longer available",
    "job posting has expired",
    "this posting has expired",
    "no longer accepting applications",
    "this job has been closed",
    "this job is closed",
]


def _workday_cxs_url(posting_id: str, url: str) -> str | None:
    parts = (posting_id or "").split(":", 3)
    if len(parts) == 4 and parts[3]:
        tenant, site, path = parts[1], parts[2], parts[3]
        m = re.search(r".(wdd+).myworkdayjobs.com", url or "")
        wd_host = m.group(1) if m else "wd5"
        return f"https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"
    return None


def verify_posting_liveness(posting: dict, timeout: float = 6.0) -> dict:
    """Verifies whether a single posting is live or closed."""
    pid = posting.get("id", "")
    url = posting.get("url", "")
    company = posting.get("company", "")
    title = posting.get("title", "")
    ats = posting.get("ats") or ""

    if not url:
        return {"id": pid, "company": company, "title": title, "status": "CLOSED", "code": 0, "reason": "empty_url"}

    # Workday CXS API check
    if pid.startswith("workday:") or "myworkdayjobs.com" in url:
        cxs_url = _workday_cxs_url(pid, url)
        if cxs_url:
            try:
                r = requests.get(cxs_url, headers={"User-Agent": UA["User-Agent"], "Accept": "application/json"}, timeout=timeout)
                if r.status_code in (404, 410):
                    return {"id": pid, "company": company, "title": title, "status": "CLOSED", "code": r.status_code, "reason": "cxs_404"}
                elif r.status_code == 403:
                    # Measured live: Workday's CXS detail endpoint 403s on
                    # postings the source's own list fetch still confirms
                    # open (see service/liveness.py's WORKDAY_GONE_STATUSES
                    # comment) -- a block, not a closure.
                    return {"id": pid, "company": company, "title": title, "status": "UNCERTAIN", "code": 403, "reason": "cxs_403_blocked"}
                elif r.status_code == 200:
                    return {"id": pid, "company": company, "title": title, "status": "ALIVE", "code": 200, "reason": "cxs_200"}
                else:
                    return {"id": pid, "company": company, "title": title, "status": "UNCERTAIN", "code": r.status_code, "reason": f"http_{r.status_code}"}
            except Exception as e:
                return {"id": pid, "company": company, "title": title, "status": "ERROR", "code": -1, "reason": str(e)}

    # Standard URL check
    try:
        r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
        if r.status_code in (404, 410):
            return {"id": pid, "company": company, "title": title, "status": "CLOSED", "code": r.status_code, "reason": f"http_{r.status_code}"}
        
        text_lower = (r.text or "").lower()
        if r.status_code == 200:
            for phrase in GONE_PHRASES:
                if phrase in text_lower:
                    return {"id": pid, "company": company, "title": title, "status": "CLOSED", "code": 200, "reason": f"phrase:{phrase}"}
            return {"id": pid, "company": company, "title": title, "status": "ALIVE", "code": 200, "reason": "ok"}
        
        return {"id": pid, "company": company, "title": title, "status": "UNCERTAIN", "code": r.status_code, "reason": f"http_{r.status_code}"}
    except Exception as e:
        return {"id": pid, "company": company, "title": title, "status": "ERROR", "code": -1, "reason": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Audit posting liveness")
    parser.add_argument("--limit", type=int, default=50, help="Number of postings to check (default: 50)")
    parser.add_argument("--workers", type=int, default=12, help="Concurrency worker threads (default: 12)")
    parser.add_argument("--input", type=str, default="data/all_postings.json", help="Path to input postings JSON")
    args = parser.parse_args()

    input_path = ROOT_DIR / args.input
    if not input_path.exists():
        print(f"Error: {input_path} not found")
        sys.exit(1)

    with open(input_path, encoding="utf-8") as f:
        postings = json.load(f)

    subset = postings[:args.limit] if args.limit > 0 else postings
    print(f"Auditing liveness for {len(subset)} postings with {args.workers} concurrent workers...")

    results = []
    alive = closed = uncertain = error = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(verify_posting_liveness, p): p for p in subset}
        for future in as_completed(futures):
            res = future.result()
            results.append(res)
            status = res["status"]
            if status == "ALIVE":
                alive += 1
            elif status == "CLOSED":
                closed += 1
            elif status == "UNCERTAIN":
                uncertain += 1
            else:
                error += 1

            badge = f"[{status:6}]"
            print(f"{badge} {str(res['code']):4} | {res['company'][:22]:22} | {res['title'][:40]:40} ({res['reason']})")

    print("\n" + "=" * 60)
    print(f"AUDIT SUMMARY (Checked: {len(subset)}):")
    print(f"  Live:      {alive} ({alive / len(subset) * 100:.1f}%)")
    print(f"  Closed:    {closed} ({closed / len(subset) * 100:.1f}%)")
    print(f"  Uncertain: {uncertain}")
    print(f"  Error:     {error}")
    print("=" * 60)


if __name__ == "__main__":
    main()
