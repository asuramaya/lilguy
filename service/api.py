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
from typing import Annotated, Optional

from fastapi import FastAPI, Query, Request, Response
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent))

from dedup import compute_company_key  # noqa: E402
from user_filter import is_too_old, load_filter, passes  # noqa: E402

from atom import render_atom  # noqa: E402

from db import cursor  # noqa: E402

ROOT = Path(__file__).parent.parent
PRESETS_DIR = ROOT / "presets"
DEFAULT_FILTERS_FILE = ROOT / "filters.yaml"
STATIC_DIR = Path(__file__).parent / "static"

# Reserved preset name meaning "apply no filter at all". Without it there
# was NO way to reach the raw corpus: /feed always applied a filter (a
# named preset, or filters.yaml as the default), so of ~4700 open
# postings the frontend could surface only the ~356 the supply-chain
# default matched -- 92% of what this project collects was unreachable
# from its own UI. That's backwards for a deliberately domain-UNFILTERED
# scraper (docs/sourcing-model.md's two-layer split makes filtering the
# READER's choice, which has to include choosing not to filter).
# Checked before any presets/ file lookup, so a presets/all.yaml could
# never shadow it -- don't add one.
ALL_POSTINGS = "all"

# Upper bound on rows pulled from Postgres for one /feed request. The
# open corpus is ~4.7k, so this shouldn't bind; it exists so a runaway
# corpus degrades into an explicit `source_truncated: true` in the
# response rather than a silently short answer.
SOURCE_ROW_CAP = 20000

app = FastAPI(title="Internship feed", description="Live feed of sourced internship postings.")


def _row_to_filter_dict(row: dict) -> dict:
    d = dict(row)
    for field in ("first_seen", "last_seen", "posted_at_ts"):
        if isinstance(d.get(field), datetime):
            d[field] = d[field].isoformat()
    return d


@app.get("/health")
def health():
    with cursor() as cur:
        cur.execute("SELECT 1")
    return {"ok": True}


# Annotated-style parameters rather than `x: str = Query(None)`, so the
# actual Python default is None. With Query() as the default value, the
# function is only callable through FastAPI -- calling it directly (as
# tests/service/test_api_feed.py does, to exercise the filtering and
# paging logic without adding an HTTP client dependency) hands the body
# a Query object where it expects a string.
@app.get("/feed")
def feed(
    preset: Annotated[Optional[str], Query(
        description="Name of a file in presets/ (without .yaml), e.g. "
                    "'operations-logistics-supply-chain'. Defaults to the root filters.yaml "
                    "if omitted. Pass 'all' to apply no filter at all and get the raw "
                    "open-postings corpus.")] = None,
    keywords_any: Annotated[Optional[str], Query(
        description="Comma-separated, overrides the preset's keywords_any")] = None,
    trusted_companies: Annotated[Optional[str], Query(
        description="Comma-separated, overrides the preset's trusted_companies")] = None,
    locations_include: Annotated[Optional[str], Query(description="Comma-separated")] = None,
    max_age_days: Annotated[Optional[int], Query(
        description="Drop postings the employer posted more than N days ago. Judged on the "
                    "provider's own date where there is one, not on when this feed first "
                    "saw the posting.")] = None,
    q: Annotated[Optional[str], Query(description="Free-text match against title and company")] = None,
    category: Annotated[Optional[str], Query(description="Exact category match")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(le=5000)] = 200,
):
    with cursor() as cur:
        # Ordered by the employer's own posting date rather than
        # first_seen (our discovery time) -- see service/posted_at.py.
        # NULLS LAST so postings whose source gave no date sink to the
        # bottom instead of heading a "newest first" list.
        cur.execute(
            "SELECT * FROM postings WHERE status = 'open' "
            "ORDER BY posted_at_ts DESC NULLS LAST, first_seen DESC LIMIT %s",
            (SOURCE_ROW_CAP,),
        )
        rows = [_row_to_filter_dict(r) for r in cur.fetchall()]

    # A cap this high should never bind, but if it ever does the caller
    # is told rather than served a silently short corpus.
    source_truncated = len(rows) >= SOURCE_ROW_CAP

    if preset == ALL_POSTINGS:
        matched = rows
    else:
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
        now = datetime.now(timezone.utc)
        matched = [r for r in rows if passes(r, spec, now)]

    # Free-text and category narrowing happen HERE, server-side, over the
    # whole corpus. They used to be done in the browser over whatever
    # page had been loaded, which quietly meant searching only the newest
    # slice: with 4711 open postings and a 1000-row page, a search for
    # "logistics" silently ignored 79% of the data and reported the
    # result as if it were complete.
    #
    # Deliberately Python rather than SQL: user_filter.passes() is the
    # single definition of what a preset means and is shared with the
    # batch pipeline, so pushing filtering into SQL would fork that logic
    # into two implementations that must agree forever. At this corpus
    # size (~5k rows) filtering in Python costs a few milliseconds, which
    # is a good trade for keeping one source of truth.
    # Applied to EVERY branch including preset='all', which is where it
    # matters most: 26% of the open corpus was posted over a year ago
    # (oldest: 2016), and before posted_at was parsed there was no way to
    # tell. A preset's own max_age_days still applies via passes(); this
    # query parameter is an additional constraint on top, not a
    # replacement for it.
    if max_age_days is not None:
        now = datetime.now(timezone.utc)
        matched = [r for r in matched if not is_too_old(r, max_age_days, now)]
    if category:
        matched = [r for r in matched if (r.get("category") or "") == category]
    if q:
        needle = q.strip().lower()
        if needle:
            matched = [
                r for r in matched
                if needle in (r.get("title") or "").lower()
                or needle in (r.get("company") or "").lower()
            ]

    total = len(matched)
    page = matched[offset:offset + limit]

    # `total` is the count BEFORE paging, so a caller can tell "this
    # preset matches 91 postings" apart from "this matches thousands and
    # you're seeing a page of them" -- without it a truncated response is
    # indistinguishable from a complete one.
    return {
        "count": len(page),
        "total": total,
        "offset": offset,
        "limit": limit,
        "source_truncated": source_truncated,
        "postings": page,
    }


# ---------------------------------------------------------------------
# Entity endpoints.
#
# COMPANY, SOURCE and ATS are three different things and this project
# had been using one word ("source") for all of them. They're separated
# here because they answer different questions and, for aggregators,
# genuinely do not coincide:
#   company -- an employer. What a reader wants. SPANS sources: the same
#              employer can arrive via its own Greenhouse board AND via
#              Muse, which is exactly why dedup.py exists.
#   source  -- one board we poll. Operational: health, cadence, failures.
#              For a direct ATS connector it happens to be 1:1 with a
#              company; for Muse one source carries thousands of
#              companies, which is why collapsing the two is wrong.
#   ats     -- the platform. Explains how data arrives and why fields
#              differ (Workday sends relative dates, Lever epoch millis).
# ---------------------------------------------------------------------


@app.get("/posting/{posting_id:path}")
def posting(posting_id: str):
    """One posting, plus the context that makes it worth opening in-app
    rather than bouncing straight out to the ATS."""
    with cursor() as cur:
        cur.execute("SELECT * FROM postings WHERE id = %s", (posting_id,))
        row = cur.fetchone()
        if not row:
            return {"error": "no such posting"}
        row = _row_to_filter_dict(row)

        # The same real job as surfaced by OTHER sources. Genuinely
        # useful rather than trivia: it's how a reader sees that a
        # posting is also on the company's own board and can choose to
        # apply there instead of through an aggregator.
        cur.execute(
            "SELECT id, company, title, location, ats, source_entry, url, status "
            "FROM postings WHERE dedup_key = %s AND dedup_key IS NOT NULL AND id <> %s "
            "ORDER BY status, first_seen",
            (row.get("dedup_key"), posting_id),
        )
        also_listed = [_row_to_filter_dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT id, title, location, posted_at_ts FROM postings "
            "WHERE company_key = %s AND company_key IS NOT NULL AND id <> %s AND status = 'open' "
            "ORDER BY posted_at_ts DESC NULLS LAST LIMIT 25",
            (row.get("company_key"), posting_id),
        )
        siblings = [_row_to_filter_dict(r) for r in cur.fetchall()]

    return {"posting": row, "also_listed": also_listed, "same_company": siblings}


@app.get("/company/{company_key}")
def company(company_key: str):
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM postings WHERE company_key = %s ORDER BY status, posted_at_ts DESC NULLS LAST",
            (company_key,),
        )
        rows = [_row_to_filter_dict(r) for r in cur.fetchall()]
        if not rows:
            return {"error": "no such company"}

        # The display name is whichever spelling the most postings use --
        # company_key deliberately merges variants ("Eaton" / "Eaton
        # Corporation"), so something has to pick which to show, and
        # majority beats first-seen because raw URL slugs tend to be the
        # earliest and the ugliest.
        names = {}
        for r in rows:
            names[r["company"]] = names.get(r["company"], 0) + 1
        display_name = max(names, key=names.get)

        # Which boards this employer is reachable through. For most
        # companies that's one; for a company that has its own board AND
        # appears on an aggregator it's several, and seeing that is the
        # point of the page.
        cur.execute(
            "SELECT DISTINCT p.source_entry, p.ats, s.id AS source_id, s.status, s.category "
            "FROM postings p LEFT JOIN sources s ON p.source_id = s.id "
            "WHERE p.company_key = %s",
            (company_key,),
        )
        reached_via = [_row_to_filter_dict(r) for r in cur.fetchall()]

    return {
        "company_key": company_key,
        "display_name": display_name,
        "name_variants": sorted(names),
        "category": rows[0].get("category"),
        "open_count": sum(1 for r in rows if r["status"] == "open"),
        "reached_via": reached_via,
        "postings": rows,
    }


@app.get("/source/{source_id}")
def source(source_id: int):
    with cursor() as cur:
        cur.execute("SELECT * FROM sources WHERE id = %s", (source_id,))
        row = cur.fetchone()
        if not row:
            return {"error": "no such source"}
        row = _row_to_filter_dict(row)

        cur.execute(
            "SELECT count(*) AS total, count(*) FILTER (WHERE status='open') AS open, "
            "count(DISTINCT company_key) AS companies "
            "FROM postings WHERE source_id = %s",
            (source_id,),
        )
        stats = cur.fetchone()

        # An AGGREGATOR source carries many employers; a direct one
        # carries exactly its own. That count is what tells the two apart
        # in the UI without hardcoding a list of aggregator names.
        cur.execute(
            "SELECT company_key, company, count(*) AS n FROM postings "
            "WHERE source_id = %s AND status='open' AND company_key IS NOT NULL "
            "GROUP BY company_key, company ORDER BY n DESC LIMIT 200",
            (source_id,),
        )
        companies = [_row_to_filter_dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT started_at, finished_at, ok, error, fetched_count, internship_count "
            "FROM scrape_runs WHERE source_id = %s ORDER BY started_at DESC LIMIT 10",
            (source_id,),
        )
        runs = [_row_to_filter_dict(r) for r in cur.fetchall()]

    # Computed from the source's OWN name rather than from its postings,
    # because a direct source with no current openings is still that
    # company -- deriving the key only from postings would drop the jump
    # button exactly when the board is empty. Meaningless for an
    # aggregator (whose sources.company is a label like "The Muse
    # (aggregator ...)"), so the UI only offers the jump when this source
    # carries a single employer.
    return {
        "source": row,
        "stats": stats,
        "companies": companies,
        "recent_runs": runs,
        "company_key": compute_company_key(row.get("company") or ""),
        "is_aggregator": (stats or {}).get("companies", 0) > 1,
    }


@app.get("/ats/{name}")
def ats(name: str):
    with cursor() as cur:
        cur.execute(
            "SELECT count(*) AS sources, count(*) FILTER (WHERE status='active') AS active "
            "FROM sources WHERE ats = %s",
            (name,),
        )
        source_stats = cur.fetchone()
        cur.execute(
            "SELECT count(*) FILTER (WHERE status='open') AS open_postings, "
            "count(DISTINCT company_key) AS companies, "
            "count(*) FILTER (WHERE status='open' AND description IS NOT NULL AND description <> '') AS with_description "
            "FROM postings WHERE ats = %s",
            (name,),
        )
        posting_stats = cur.fetchone()
        cur.execute(
            "SELECT id, company, category, status, consecutive_failures, last_scraped_at "
            "FROM sources WHERE ats = %s ORDER BY company LIMIT 600",
            (name,),
        )
        sources_on_platform = [_row_to_filter_dict(r) for r in cur.fetchall()]

    if not source_stats["sources"] and not posting_stats["open_postings"]:
        return {"error": "no such ats"}
    return {"ats": name, "source_stats": source_stats, "posting_stats": posting_stats,
            "sources": sources_on_platform}


# Deliberately reuses feed() rather than reimplementing the query: a
# subscriber must get exactly what the same parameters would give them in
# the web UI, and two code paths would drift. Placed before the "/" mount
# like every other explicit route.
@app.get("/feed.atom")
def feed_atom(
    request: Request,
    preset: Annotated[Optional[str], Query(description="Same preset names as /feed, including 'all'")] = None,
    q: Annotated[Optional[str], Query(description="Free-text match against title and company")] = None,
    category: Annotated[Optional[str], Query(description="Exact category match")] = None,
    max_age_days: Annotated[Optional[int], Query(description="Only postings the employer posted within N days")] = None,
    # Far smaller default than /feed: a feed reader wants the recent
    # head of the list, not a bulk export, and every entry is re-parsed
    # by the client on every poll.
    limit: Annotated[int, Query(le=500)] = 50,
):
    result = feed(preset=preset, q=q, category=category, max_age_days=max_age_days, limit=limit)
    if "error" in result:
        return Response(content=f"<error>{result['error']}</error>", status_code=404,
                        media_type="application/xml")

    label = {None: "default filter", ALL_POSTINGS: "all postings"}.get(preset, preset)
    bits = [b for b in (q and f"matching '{q}'", category, max_age_days and f"posted within {max_age_days}d") if b]
    title = "Internship Feed — " + label + (f" ({', '.join(bits)})" if bits else "")

    return Response(
        content=render_atom(
            result["postings"],
            title=title,
            self_url=str(request.url),
            # Distinct id per distinct query, so subscribing to two
            # different presets doesn't look like one feed changing its
            # mind to a reader keying on feed id.
            feed_slug="feed/" + (request.url.query or "default"),
        ),
        media_type="application/atom+xml; charset=utf-8",
    )


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


class RevalidatingStaticFiles(StaticFiles):
    """StaticFiles that asks browsers to revalidate instead of guessing.

    StaticFiles already sends ETag and Last-Modified, but sends NO
    Cache-Control -- and with no explicit freshness a browser falls back
    to HEURISTIC freshness (RFC 9111 section 4.2.2, commonly ~10% of the
    time since Last-Modified) and will serve a stale copy WITHOUT
    revalidating. Confirmed live: after deploying a rewritten
    index.html, curl against the origin returned the new bytes while the
    browser kept rendering the previous build until the URL's query
    string was changed. That means every UI deploy was effectively
    invisible to anyone who had already loaded the page.

    "no-cache" does not mean "do not store" -- it means "store, but
    revalidate before reuse". The ETag above turns that revalidation
    into a cheap 304 with no body, so this costs one conditional request
    per load rather than re-downloading the page.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


# Registered last on purpose -- Starlette matches routes in registration
# order, so the explicit /health, /feed, /sources, /candidates routes
# above always win first. This mount only catches what's left ("/" and
# any other static asset path), serving service/static/index.html as a
# minimal read-only UI over the same three JSON endpoints. No separate
# build step / npm dependency -- plain HTML+JS, fetched at runtime from
# whatever origin the page itself was served from.
app.mount("/", RevalidatingStaticFiles(directory=str(STATIC_DIR), html=True), name="static")
