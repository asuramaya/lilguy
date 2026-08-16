"""Serves the feed live from Postgres -- this is what actually makes it
"real time" rather than "daily batch, faster": FEED.md was a snapshot
rendered once a day and committed to git; /feed is a query that runs
against whatever the scheduler most recently wrote, which could be
seconds old.

Filtering logic is untouched from the batch pipeline -- user_filter.py's
`passes()` is pure (a dict in, a bool out) and doesn't care whether that
dict came from data/all_postings.json or a Postgres row, so it's reused
here exactly as-is rather than reimplemented against SQL.

Run with: uvicorn service.api:app --host 0.0.0.0 --port 8000
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent))

from user_filter import load_filter, passes  # noqa: E402

from db import cursor  # noqa: E402

ROOT = Path(__file__).parent.parent
PRESETS_DIR = ROOT / "presets"
DEFAULT_FILTERS_FILE = ROOT / "filters.yaml"
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Internship feed", description="Live feed of sourced internship postings.")


def _row_to_filter_dict(row: dict) -> dict:
    d = dict(row)
    for field in ("first_seen", "last_seen"):
        if isinstance(d.get(field), datetime):
            d[field] = d[field].isoformat()
    return d


@app.get("/health")
def health():
    with cursor() as cur:
        cur.execute("SELECT 1")
    return {"ok": True}


@app.get("/feed")
def feed(
    preset: str = Query(None, description="Name of a file in presets/ (without .yaml), e.g. "
                                            "'operations-logistics-supply-chain'. Defaults to the "
                                            "root filters.yaml if omitted."),
    keywords_any: str = Query(None, description="Comma-separated, overrides the preset's keywords_any"),
    trusted_companies: str = Query(None, description="Comma-separated, overrides the preset's trusted_companies"),
    locations_include: str = Query(None, description="Comma-separated"),
    max_age_days: int = Query(None),
    limit: int = Query(200, le=2000),
):
    filter_path = (PRESETS_DIR / f"{preset}.yaml") if preset else DEFAULT_FILTERS_FILE
    if not filter_path.exists():
        return {"error": f"no such preset '{preset}'"}
    spec = load_filter(str(filter_path))

    if keywords_any is not None:
        spec["keywords_any"] = [k.strip() for k in keywords_any.split(",") if k.strip()]
    if trusted_companies is not None:
        spec["trusted_companies"] = [k.strip() for k in trusted_companies.split(",") if k.strip()]
    if locations_include is not None:
        spec["locations_include"] = [k.strip() for k in locations_include.split(",") if k.strip()]
    if max_age_days is not None:
        spec["max_age_days"] = max_age_days

    with cursor() as cur:
        cur.execute("SELECT * FROM postings WHERE status = 'open' ORDER BY first_seen DESC LIMIT 5000")
        rows = [_row_to_filter_dict(r) for r in cur.fetchall()]

    now = datetime.now(timezone.utc)
    matched = [r for r in rows if passes(r, spec, now)][:limit]
    return {"count": len(matched), "postings": matched}


@app.get("/sources")
def sources():
    with cursor() as cur:
        cur.execute(
            "SELECT company, ats, category, status, added_by, scrape_interval_seconds, "
            "consecutive_failures, last_scrape_status, last_scraped_at, next_scrape_at "
            "FROM sources ORDER BY company"
        )
        return {"sources": cur.fetchall()}


@app.get("/events")
def events(limit: int = Query(50, le=500)):
    """Backs the frontend's 'N new since you last looked' indicator (see
    docs/service-architecture.md's 'Staying informed' section for why this
    exists instead of an email/webhook notifier -- no persistent process
    exists to drive one, and this project holds no SMTP/webhook
    credentials to send with). Written by scheduler.py (source disabled),
    discovery.py (promoted / reinstated), and scripts/restore_test_backup.sh
    (backup verification result) -- see service/schema.sql's own comment
    on the events table for what does and doesn't get logged here.
    """
    with cursor() as cur:
        cur.execute(
            "SELECT id, kind, company, detail, created_at FROM events ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        return {"events": [_row_to_filter_dict(r) for r in cur.fetchall()]}


@app.get("/duplicates")
def duplicates(limit: int = Query(500, le=5000)):
    """Audit view over what dedup.py's sweep actually caught -- deliberately
    NOT run through user_filter.py's domain matching like /feed is, since
    this is for sanity-checking the sweep itself (did it collapse the right
    rows together?), not for reading as a feed. Self-joins `duplicate_of`
    back to the canonical posting's own company/title/source so a duplicate
    row reads as "X is a duplicate of Y", not just an opaque id.
    """
    with cursor() as cur:
        cur.execute(
            """
            SELECT d.id, d.company, d.title, d.location, d.ats AS source, d.first_seen,
                   c.id AS canonical_id, c.company AS canonical_company,
                   c.title AS canonical_title, c.ats AS canonical_source
            FROM postings d
            LEFT JOIN postings c ON c.id = d.duplicate_of
            WHERE d.status = 'duplicate'
            ORDER BY d.first_seen DESC
            LIMIT %s
            """,
            (limit,),
        )
        return {"duplicates": [_row_to_filter_dict(r) for r in cur.fetchall()]}


@app.get("/candidates")
def candidates():
    with cursor() as cur:
        cur.execute(
            "SELECT company, ats, review_status, evidence, checked_at, next_check_at "
            "FROM discovery_candidates ORDER BY checked_at DESC NULLS FIRST"
        )
        return {"candidates": cur.fetchall()}


# Registered last on purpose -- Starlette matches routes in registration
# order, so the explicit /health, /feed, /sources, /candidates routes
# above always win first. This mount only catches what's left ("/" and
# any other static asset path), serving service/static/index.html as a
# minimal read-only UI over the same three JSON endpoints. No separate
# build step / npm dependency -- plain HTML+JS, fetched at runtime from
# whatever origin the page itself was served from.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
