"""Two things worth separating: whether a NAME looks unresolved and worth
fixing (pure functions, no DB needed), and whether the SWEEP correctly
finds only genuinely-unresolved sources and leaves everything else
alone (DB-backed, since that's a real WHERE clause against real rows).
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import psycopg2.extras
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

import workday_descriptions as wd  # noqa: E402

# --- pure functions, no DB -------------------------------------------------


def test_a_legal_suffix_is_stripped():
    # The live example this whole fix is for: "ms"'s own detail payload
    # names its hiring organization "711 MS Smith Barney, LLC", not
    # "Morgan Stanley" -- this is as far as cleanup goes deliberately,
    # see workday_descriptions.py's own comment on why guessing further
    # is out of scope.
    assert wd._clean_company_name("711 MS Smith Barney, LLC") == "711 MS Smith Barney"


def test_stacked_suffixes_are_all_stripped():
    assert wd._clean_company_name("Foo Corp, Inc.") == "Foo"


def test_a_name_with_no_suffix_is_unchanged():
    assert wd._clean_company_name("Acme Robotics") == "Acme Robotics"


def test_empty_input_is_handled():
    assert wd._clean_company_name("") == ""
    assert wd._clean_company_name(None) == ""


def test_maybe_fix_company_only_fires_when_the_source_still_equals_its_tenant():
    source = wd.WorkdayDescriptions()
    row = {"tenant": "ms", "source_company": "ms"}
    payload = {"hiringOrganization": {"name": "711 MS Smith Barney, LLC"}}
    assert source.maybe_fix_company(row, payload) == "711 MS Smith Barney"


def test_maybe_fix_company_leaves_an_already_resolved_source_alone():
    # "Nexstar Media Group, Inc." already differs from its tenant --
    # a hand-entered or previously-corrected name outranks a guess from
    # one job's detail page, even if that guess happens to differ too.
    source = wd.WorkdayDescriptions()
    row = {"tenant": "nexstar", "source_company": "Nexstar Media Group, Inc."}
    payload = {"hiringOrganization": {"name": "Some Other Legal Entity, LLC"}}
    assert source.maybe_fix_company(row, payload) is None


def test_maybe_fix_company_declines_when_the_payload_has_nothing_better():
    source = wd.WorkdayDescriptions()
    row = {"tenant": "ms", "source_company": "ms"}
    assert source.maybe_fix_company(row, {}) is None
    assert source.maybe_fix_company(row, {"hiringOrganization": {"name": "MS"}}) is None


# --- the sweep, against a real DB ------------------------------------------

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ, reason="needs a scratch Postgres via DATABASE_URL"
)

import db  # noqa: E402
import company_resolution as cr  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    db.init_schema()
    with db.cursor() as cur:
        cur.execute("TRUNCATE postings, sources RESTART IDENTITY CASCADE")
    yield


def _source(company, tenant, status="active"):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (company, ats, config, status) VALUES (%s, 'workday', %s, %s) RETURNING id",
            (company, psycopg2.extras.Json({"tenant": tenant, "wd_host": "wd5", "site": "External"}), status),
        )
        return cur.fetchone()["id"]


def _open_posting(source_id, tenant, path="/job/Somewhere/Intern_JR1"):
    pid = f"workday:{tenant}:External:{path}"
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO postings (id, source_id, source_entry, company, title, location, url, ats,
                                   category, status, first_seen, last_seen)
            VALUES (%s, %s, %s, %s, 'Intern', 'Remote', 'https://x', 'workday', '', 'open', now(), now())
            """,
            (pid, source_id, tenant, tenant),
        )
    return pid


class FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _session(handler):
    return SimpleNamespace(get=lambda url, **kw: handler(url))


def _company(source_id):
    with db.cursor() as cur:
        cur.execute("SELECT company FROM sources WHERE id = %s", (source_id,))
        return cur.fetchone()["company"]


def test_a_source_whose_company_still_equals_its_tenant_gets_fixed():
    sid = _source("ms", "ms")
    _open_posting(sid, "ms")
    out = cr.run(limit=5, pace=0, session=_session(
        lambda url: FakeResp(200, {"hiringOrganization": {"name": "711 MS Smith Barney, LLC"}})))
    assert out["fixed"] == 1
    assert _company(sid) == "711 MS Smith Barney"


def test_a_source_with_a_real_name_already_is_never_claimed():
    sid = _source("Nexstar Media Group, Inc.", "nexstar")
    _open_posting(sid, "nexstar")
    calls = []
    out = cr.run(limit=5, pace=0, session=_session(lambda url: calls.append(url) or FakeResp(200, {})))
    assert out["attempted"] == 0
    assert calls == []
    assert _company(sid) == "Nexstar Media Group, Inc."


def test_a_source_with_no_open_postings_is_skipped_not_crashed():
    sid = _source("ms", "ms")
    out = cr.run(limit=5, pace=0, session=_session(lambda url: FakeResp(200, {})))
    assert out["attempted"] == 1
    assert out["skipped"] == 1
    assert out["fixed"] == 0
    assert _company(sid) == "ms"   # still unresolved -- nothing to fetch from yet


def test_a_fixed_source_is_not_reclaimed_on_a_second_pass():
    sid = _source("ms", "ms")
    _open_posting(sid, "ms")
    cr.run(limit=5, pace=0, session=_session(
        lambda url: FakeResp(200, {"hiringOrganization": {"name": "711 MS Smith Barney, LLC"}})))
    calls = []
    out = cr.run(limit=5, pace=0, session=_session(lambda url: calls.append(url) or FakeResp(200, {})))
    assert out["attempted"] == 0
    assert calls == []
