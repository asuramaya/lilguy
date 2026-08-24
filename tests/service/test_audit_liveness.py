"""audit_liveness.py exists to catch drift service/liveness.py's own
continuous sweep can't notice about itself -- a platform's close signal
changing, a newly-added ATS never getting a bespoke check. These tests
cover the DB-facing parts unique to the audit: stratified sampling per
ATS, and that a confirmed-dead row it finds actually closes through the
SAME path the sweep uses. check_posting_status's own per-platform logic
is covered by test_liveness.py already; re-testing it here would just
be a second copy of those same cases.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import psycopg2.extras
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ, reason="needs a scratch Postgres via DATABASE_URL"
)

import db  # noqa: E402
import audit_liveness  # noqa: E402

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _clean_db():
    db.init_schema()
    with db.cursor() as cur:
        cur.execute("TRUNCATE postings, sources, events RESTART IDENTITY CASCADE")
    yield


def _source(company, ats):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (company, ats, config, status) "
            "VALUES (%s, %s, %s, 'active') "
            "ON CONFLICT (company, ats) DO UPDATE SET status = 'active' RETURNING id",
            (company, ats, psycopg2.extras.Json({})),
        )
        return cur.fetchone()["id"]


def _posting(pid, ats, days_old=400, status="open"):
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_id, source_entry, company, title, location, url, ats,
                                   category, status, posted_at_ts, first_seen, last_seen)
            VALUES (%s, %s, 'Acme Careers', 'Acme', 'Ops Intern', 'Remote', %s, %s,
                    'Logistics', %s, %s, now(), now())
            """,
            (pid, _source("Acme", ats), f"https://example.com/jobs/{pid}", ats, status,
             NOW - timedelta(days=days_old)),
        )


def _row(pid):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM postings WHERE id = %s", (pid,))
        return cur.fetchone()


def _session(handler):
    return SimpleNamespace(get=lambda url, **kw: handler(url))


class FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code
        self.history = []
        self.url = ""
        self.text = ""


def test_sample_from_db_only_returns_open_postings():
    _posting("open-one", "muse", status="open")
    _posting("closed-one", "muse", status="closed")
    rows = audit_liveness._sample_from_db(sample_size=10)
    ids = {r["id"] for r in rows}
    assert "open-one" in ids
    assert "closed-one" not in ids


def test_sample_from_db_stratifies_across_every_ats():
    # A small platform (lever, 1 posting) must not go invisible just
    # because a big one (muse, 5 postings) would otherwise fill the
    # whole sample -- each ATS gets its OWN limit, not a shared pool.
    for i in range(5):
        _posting(f"muse-{i}", "muse")
    _posting("lever-1", "lever")
    rows = audit_liveness._sample_from_db(sample_size=10)
    ats_present = {r["ats"] for r in rows}
    assert ats_present == {"muse", "lever"}
    assert any(r["id"] == "lever-1" for r in rows)


def test_sample_from_db_respects_per_ats_limit():
    for i in range(8):
        _posting(f"muse-{i}", "muse")
    rows = audit_liveness._sample_from_db(sample_size=3)
    assert len([r for r in rows if r["ats"] == "muse"]) == 3


def test_run_audit_tallies_dead_and_alive_per_ats():
    _posting("dead-one", "muse")
    _posting("alive-one", "muse")
    rows = [
        {"id": "dead-one", "url": "x", "company": "Acme", "ats": "muse"},
        {"id": "alive-one", "url": "y", "company": "Acme", "ats": "muse"},
    ]
    responses = {"x": FakeResp(404), "y": FakeResp(200)}
    result = audit_liveness.run_audit(rows, http=_session(lambda u: responses[u]), workers=2)
    bucket = result["per_ats"]["muse"]
    assert bucket == {"checked": 2, "dead": 1, "alive": 1, "uncertain": 0}
    assert result["dead_rows"] == [("dead-one", "Acme", 404)]


def test_run_audits_dead_rows_close_through_the_same_path_the_sweep_uses():
    # The audit never re-implements closing -- it hands its findings to
    # liveness.close_dead_postings, the exact function _record's own
    # close branch backs, so this is really testing that the wiring
    # between the two files stays connected, not re-testing _record.
    _posting("dead-one", "muse")
    rows = [{"id": "dead-one", "url": "x", "company": "Acme", "ats": "muse"}]
    result = audit_liveness.run_audit(rows, http=_session(lambda u: FakeResp(404)), workers=1)
    assert _row("dead-one")["status"] == "open"  # run_audit itself never writes
    from liveness import close_dead_postings
    close_dead_postings(result["dead_rows"])
    assert _row("dead-one")["status"] == "closed"


def test_run_audit_uncertain_never_counted_as_dead():
    _posting("blocked-one", "muse")
    rows = [{"id": "blocked-one", "url": "x", "company": "Acme", "ats": "muse"}]
    result = audit_liveness.run_audit(rows, http=_session(lambda u: FakeResp(403)), workers=1)
    bucket = result["per_ats"]["muse"]
    assert bucket["dead"] == 0
    assert bucket["uncertain"] == 1
    assert result["dead_rows"] == []
