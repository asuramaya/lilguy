"""Exercises the actual DB reconciliation logic (upsert, close-on-absence,
probation -> active promotion, probation -> rejected on failure) against
a real Postgres -- the part of the redesign that's genuinely new and
genuinely worth proving against a real database rather than a mock,
since it's SQL correctness under test, not connector correctness (the
four connectors are already covered by tests/test_*_connector.py and
proven against live data this session).

Needs DATABASE_URL pointing at a scratch Postgres (schema is created
fresh in it) -- skipped automatically if that isn't set, so this doesn't
break `pytest tests/` for anyone without Postgres available locally. CI
guidance: docker run --rm -e POSTGRES_PASSWORD=x -p 5432:5432 postgres,
then DATABASE_URL=postgresql://postgres:x@localhost:5432/postgres.
"""
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ, reason="needs a scratch Postgres via DATABASE_URL"
)

import db  # noqa: E402
import scheduler  # noqa: E402


def fake_posting(id_, company, title, source="greenhouse", category="Test"):
    return SimpleNamespace(id=id_, company=company, title=title, location="Remote", url=f"https://x/{id_}",
                            source=source, category=category, posted_at=None, description_snippet="",
                            description="")


@pytest.fixture(autouse=True)
def _clean_db():
    db.init_schema()
    with db.cursor() as cur:
        cur.execute("TRUNCATE postings, scrape_runs, sources, discovery_candidates RESTART IDENTITY CASCADE")
    yield


def _insert_source(company, status="active", last_scraped_at=None):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (company, ats, config, status, last_scraped_at) "
            "VALUES (%s, 'greenhouse', %s, %s, %s) RETURNING id",
            (company, __import__("psycopg2").extras.Json({"company": company, "ats": "greenhouse", "token": "x"}),
             status, last_scraped_at),
        )
        return cur.fetchone()["id"]


def test_new_postings_are_inserted_as_open(monkeypatch):
    source_id = _insert_source("Acme")
    row = {"id": source_id, "company": "Acme", "ats": "greenhouse", "config": {}, "status": "active",
           "consecutive_failures": 0, "last_scraped_at": None}

    fake_postings = [fake_posting(f"gh:acme:{uuid.uuid4()}", "Acme", "Supply Chain Intern")]
    monkeypatch.setitem(scheduler.CONNECTORS, "greenhouse", lambda: SimpleNamespace(fetch=lambda cfg: fake_postings))

    result = scheduler.run_one(row)
    assert result["ok"] is True
    assert result["new"] == 1

    with db.cursor() as cur:
        cur.execute("SELECT status FROM postings WHERE id = %s", (fake_postings[0].id,))
        assert cur.fetchone()["status"] == "open"


def test_postings_absent_from_next_fetch_are_closed(monkeypatch):
    source_id = _insert_source("Acme")
    row = {"id": source_id, "company": "Acme", "ats": "greenhouse", "config": {}, "status": "active",
           "consecutive_failures": 0, "last_scraped_at": None}

    p1 = fake_posting("gh:acme:1", "Acme", "Supply Chain Intern")
    p2 = fake_posting("gh:acme:2", "Acme", "Logistics Intern")
    monkeypatch.setitem(scheduler.CONNECTORS, "greenhouse", lambda: SimpleNamespace(fetch=lambda cfg: [p1, p2]))
    scheduler.run_one(row)

    # Second fetch only returns p1 -- p2 has closed/filled.
    monkeypatch.setitem(scheduler.CONNECTORS, "greenhouse", lambda: SimpleNamespace(fetch=lambda cfg: [p1]))
    result = scheduler.run_one(row)
    assert result["closed"] == 1

    with db.cursor() as cur:
        cur.execute("SELECT id, status FROM postings ORDER BY id")
        statuses = {r["id"]: r["status"] for r in cur.fetchall()}
    assert statuses["gh:acme:1"] == "open"
    assert statuses["gh:acme:2"] == "closed"


def test_failed_fetch_does_not_touch_existing_postings(monkeypatch):
    # The core guarantee this whole redesign has to preserve: a source
    # failing to fetch is not the same fact as it reporting zero
    # postings (see docs/sourcing-model.md's own writeup of this bug).
    source_id = _insert_source("Acme")
    row = {"id": source_id, "company": "Acme", "ats": "greenhouse", "config": {}, "status": "active",
           "consecutive_failures": 0, "last_scraped_at": None}

    p1 = fake_posting("gh:acme:1", "Acme", "Supply Chain Intern")
    monkeypatch.setitem(scheduler.CONNECTORS, "greenhouse", lambda: SimpleNamespace(fetch=lambda cfg: [p1]))
    scheduler.run_one(row)

    def _raise(cfg):
        raise RuntimeError("transient network error")

    monkeypatch.setitem(scheduler.CONNECTORS, "greenhouse", lambda: SimpleNamespace(fetch=_raise))
    result = scheduler.run_one({**row, "consecutive_failures": 0})
    assert result["ok"] is False

    with db.cursor() as cur:
        cur.execute("SELECT status FROM postings WHERE id = 'gh:acme:1'")
        assert cur.fetchone()["status"] == "open"  # untouched, not closed


def test_probation_source_promotes_to_active_on_second_success(monkeypatch):
    from datetime import datetime, timezone

    source_id = _insert_source("Acme", status="probation")
    p1 = fake_posting("gh:acme:1", "Acme", "Supply Chain Intern")
    monkeypatch.setitem(scheduler.CONNECTORS, "greenhouse", lambda: SimpleNamespace(fetch=lambda cfg: [p1]))

    first = {"id": source_id, "company": "Acme", "ats": "greenhouse", "config": {}, "status": "probation",
             "consecutive_failures": 0, "last_scraped_at": None}  # first-ever attempt
    r1 = scheduler.run_one(first)
    assert r1["ok"] is True
    assert not r1.get("promoted")

    with db.cursor() as cur:
        cur.execute("SELECT status FROM sources WHERE id = %s", (source_id,))
        assert cur.fetchone()["status"] == "probation"  # still probation after ONE success

    second = {"id": source_id, "company": "Acme", "ats": "greenhouse", "config": {}, "status": "probation",
              "consecutive_failures": 0, "last_scraped_at": datetime.now(timezone.utc)}  # a prior cycle happened
    r2 = scheduler.run_one(second)
    assert r2["ok"] is True
    assert r2["promoted"] is True

    with db.cursor() as cur:
        cur.execute("SELECT status FROM sources WHERE id = %s", (source_id,))
        assert cur.fetchone()["status"] == "active"


def test_probation_source_rejected_on_failed_confirmation(monkeypatch):
    from datetime import datetime, timezone

    source_id = _insert_source("Acme", status="probation")

    def _raise(cfg):
        raise RuntimeError("board moved")

    monkeypatch.setitem(scheduler.CONNECTORS, "greenhouse", lambda: SimpleNamespace(fetch=_raise))
    row = {"id": source_id, "company": "Acme", "ats": "greenhouse", "config": {}, "status": "probation",
           "consecutive_failures": 0, "last_scraped_at": datetime.now(timezone.utc)}
    result = scheduler.run_one(row)
    assert result["ok"] is False

    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM sources WHERE id = %s", (source_id,))
        assert cur.fetchone()["n"] == 0  # removed, not left dangling in probation forever
        cur.execute("SELECT review_status FROM discovery_candidates WHERE company = 'Acme'")
        assert cur.fetchone()["review_status"] == "rejected"


def test_active_source_disables_after_repeated_failures(monkeypatch):
    source_id = _insert_source("Acme", status="active")

    def _raise(cfg):
        raise RuntimeError("dead board")

    monkeypatch.setitem(scheduler.CONNECTORS, "greenhouse", lambda: SimpleNamespace(fetch=_raise))
    row = {"id": source_id, "company": "Acme", "ats": "greenhouse", "config": {}, "status": "active",
           "consecutive_failures": scheduler.FAILURE_DISABLE_THRESHOLD - 1, "last_scraped_at": None}
    scheduler.run_one(row)

    with db.cursor() as cur:
        cur.execute("SELECT status, consecutive_failures FROM sources WHERE id = %s", (source_id,))
        r = cur.fetchone()
    assert r["status"] == "disabled"
    assert r["consecutive_failures"] == scheduler.FAILURE_DISABLE_THRESHOLD


def test_recategorized_source_updates_existing_open_postings(monkeypatch):
    # Regression: confirmed live -- apply_categorization.py updates
    # sources.config->>'category' AND ->>'company', and every later
    # scrape fetches the corrected values into p.category/p.company, but
    # the ON CONFLICT DO UPDATE clause didn't list either in its SET
    # list, so an already-open posting kept whatever values it was first
    # inserted with forever. 101/356 open postings in the live default
    # feed still showed 'Uncategorized', and 5253 postings had a stale
    # company, after the sources table itself was fully corrected.
    source_id = _insert_source("Acme")
    row = {"id": source_id, "company": "Acme", "ats": "greenhouse", "config": {}, "status": "active",
           "consecutive_failures": 0, "last_scraped_at": None}

    posting_id = f"gh:acme:{uuid.uuid4()}"
    first_fetch = [fake_posting(posting_id, "acmecorp", "Supply Chain Intern", category="Uncategorized")]
    monkeypatch.setitem(scheduler.CONNECTORS, "greenhouse", lambda: SimpleNamespace(fetch=lambda cfg: first_fetch))
    scheduler.run_one(row)

    with db.cursor() as cur:
        cur.execute("SELECT category, company FROM postings WHERE id = %s", (posting_id,))
        r = cur.fetchone()
        assert r["category"] == "Uncategorized"
        assert r["company"] == "acmecorp"

    recategorized_fetch = [fake_posting(posting_id, "Acme Corporation", "Supply Chain Intern", category="Logistics")]
    monkeypatch.setitem(scheduler.CONNECTORS, "greenhouse",
                         lambda: SimpleNamespace(fetch=lambda cfg: recategorized_fetch))
    scheduler.run_one(row)

    with db.cursor() as cur:
        cur.execute("SELECT category, company FROM postings WHERE id = %s", (posting_id,))
        r = cur.fetchone()
        assert r["category"] == "Logistics"
        assert r["company"] == "Acme Corporation"


def _fake_posting_with_date(id_, posted_at):
    return SimpleNamespace(id=id_, company="Acme", title="Supply Chain Intern", location="Remote",
                            url=f"https://x/{id_}", source="greenhouse", category="Test",
                            posted_at=posted_at, description_snippet="", description="")


def test_exact_posted_at_is_parsed_into_a_real_timestamp(monkeypatch):
    source_id = _insert_source("Acme")
    row = {"id": source_id, "company": "Acme", "ats": "greenhouse", "config": {}, "status": "active",
           "consecutive_failures": 0, "last_scraped_at": None}
    p = _fake_posting_with_date("gh:acme:d1", "2026-07-09T10:58:08-04:00")
    monkeypatch.setitem(scheduler.CONNECTORS, "greenhouse", lambda: SimpleNamespace(fetch=lambda cfg: [p]))
    scheduler.run_one(row)

    with db.cursor() as cur:
        cur.execute("SELECT posted_at_ts, posted_at_approx FROM postings WHERE id = 'gh:acme:d1'")
        r = cur.fetchone()
    assert r["posted_at_ts"].year == 2026 and r["posted_at_ts"].month == 7
    assert r["posted_at_approx"] is False


def test_saturating_relative_date_keeps_the_earliest_estimate(monkeypatch):
    # "Posted 30+ Days Ago" is an UPPER bound on the posted date, not a
    # measurement. Re-resolving it against each new scrape would push
    # that bound forward forever, so a six-month-old posting would keep
    # reporting as 30 days old -- precisely the staleness this column
    # exists to surface. The earliest estimate is the tightest bound.
    source_id = _insert_source("Acme")
    row = {"id": source_id, "company": "Acme", "ats": "greenhouse", "config": {}, "status": "active",
           "consecutive_failures": 0, "last_scraped_at": None}
    p = _fake_posting_with_date("gh:acme:d2", "Posted 30+ Days Ago")
    monkeypatch.setitem(scheduler.CONNECTORS, "greenhouse", lambda: SimpleNamespace(fetch=lambda cfg: [p]))

    scheduler.run_one(row)
    with db.cursor() as cur:
        cur.execute("SELECT posted_at_ts FROM postings WHERE id = 'gh:acme:d2'")
        first = cur.fetchone()["posted_at_ts"]

    scheduler.run_one(row)  # scraped again later; same saturating string
    with db.cursor() as cur:
        cur.execute("SELECT posted_at_ts, posted_at_approx FROM postings WHERE id = 'gh:acme:d2'")
        r = cur.fetchone()

    assert r["posted_at_ts"] <= first, "bound drifted forward on re-scrape"
    assert r["posted_at_approx"] is True


def test_unparseable_posted_at_leaves_the_timestamp_null(monkeypatch):
    source_id = _insert_source("Acme")
    row = {"id": source_id, "company": "Acme", "ats": "greenhouse", "config": {}, "status": "active",
           "consecutive_failures": 0, "last_scraped_at": None}
    p = _fake_posting_with_date("gh:acme:d3", "sometime last spring")
    monkeypatch.setitem(scheduler.CONNECTORS, "greenhouse", lambda: SimpleNamespace(fetch=lambda cfg: [p]))
    scheduler.run_one(row)

    with db.cursor() as cur:
        cur.execute("SELECT posted_at_ts FROM postings WHERE id = 'gh:acme:d3'")
        assert cur.fetchone()["posted_at_ts"] is None
