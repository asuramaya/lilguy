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


def test_maybe_fix_company_is_disabled():
    # Confirmed live 2026-08-18: a single posting's hiringOrganization
    # is not reliable enough for this to auto-apply unattended (it
    # replaced "3M" with a Shanghai subsidiary's legal name, among
    # others) -- see workday_descriptions.py's own comment on the
    # disabled body. This asserts the OFF state itself, so a future
    # re-enable has to touch this test deliberately rather than leaving
    # a stale assertion nobody update.
    source = wd.WorkdayDescriptions()
    row = {"tenant": "ms", "source_company": "ms"}
    payload = {"hiringOrganization": {"name": "711 MS Smith Barney, LLC"}}
    assert source.maybe_fix_company(row, payload) is None


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


def test_a_source_gets_fixed_when_most_sampled_postings_agree():
    sid = _source("ms", "ms")
    _open_posting(sid, "ms", path="/job/A/Intern_JR1")
    _open_posting(sid, "ms", path="/job/B/Intern_JR2")
    _open_posting(sid, "ms", path="/job/C/Intern_JR3")
    out = cr.run(limit=5, pace=0, session=_session(
        lambda url: FakeResp(200, {"hiringOrganization": {"name": "711 MS Smith Barney, LLC"}})))
    assert out["fixed"] == 1
    assert _company(sid) == "711 MS Smith Barney"


def test_a_source_is_left_alone_when_postings_disagree_with_no_majority():
    # Confirmed live 2026-08-18, the whole reason this sweep now votes
    # instead of trusting one posting: a multinational's regional
    # postings each report their OWN legal entity, not the parent
    # company. Three names, none with a majority -- unresolved-but-
    # honest beats resolved-but-wrong.
    sid = _source("3m", "3m")
    _open_posting(sid, "3m", path="/job/A/Intern_JR1")
    _open_posting(sid, "3m", path="/job/B/Intern_JR2")
    _open_posting(sid, "3m", path="/job/C/Intern_JR3")
    names = iter(["CHN 3M Specialty Materials (Shanghai)", "COP AU Op Pty", "70032 Blackstone Europe LLP"])
    out = cr.run(limit=5, pace=0, session=_session(
        lambda url: FakeResp(200, {"hiringOrganization": {"name": next(names)}})))
    assert out["fixed"] == 0
    assert out["skipped"] == 1
    assert _company(sid) == "3m"


def test_a_source_with_fewer_than_min_samples_is_left_unresolved():
    # A single agreeing answer is still just one posting's say-so -- the
    # exact failure mode this redesign exists to avoid, so it isn't
    # enough on its own even with no disagreement to weigh against it.
    sid = _source("ms", "ms")
    _open_posting(sid, "ms")
    out = cr.run(limit=5, pace=0, session=_session(
        lambda url: FakeResp(200, {"hiringOrganization": {"name": "711 MS Smith Barney, LLC"}})))
    assert out["fixed"] == 0
    assert out["skipped"] == 1
    assert _company(sid) == "ms"


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
    _open_posting(sid, "ms", path="/job/A/Intern_JR1")
    _open_posting(sid, "ms", path="/job/B/Intern_JR2")
    cr.run(limit=5, pace=0, session=_session(
        lambda url: FakeResp(200, {"hiringOrganization": {"name": "711 MS Smith Barney, LLC"}})))
    calls = []
    out = cr.run(limit=5, pace=0, session=_session(lambda url: calls.append(url) or FakeResp(200, {})))
    assert out["attempted"] == 0
    assert calls == []


def test_a_few_failed_fetches_dont_prevent_a_majority_among_the_rest():
    # A missing vote (network error, non-200, malformed body) shouldn't
    # by itself sink an otherwise-clear majority among the postings that
    # DID answer.
    sid = _source("ms", "ms")
    _open_posting(sid, "ms", path="/job/A/Intern_JR1")
    _open_posting(sid, "ms", path="/job/B/Intern_JR2")
    _open_posting(sid, "ms", path="/job/C/Intern_JR3")
    responses = iter([FakeResp(200, {"hiringOrganization": {"name": "711 MS Smith Barney, LLC"}}),
                       FakeResp(500), FakeResp(200, {"hiringOrganization": {"name": "711 MS Smith Barney, LLC"}})])
    out = cr.run(limit=5, pace=0, session=_session(lambda url: next(responses)))
    assert out["fixed"] == 1
    assert _company(sid) == "711 MS Smith Barney"
