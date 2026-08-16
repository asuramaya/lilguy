import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

from dedup import compute_dedup_key  # noqa: E402


def test_identical_inputs_produce_identical_key():
    a = compute_dedup_key("Acme Corp", "Supply Chain Intern", "Austin, TX")
    b = compute_dedup_key("Acme Corp", "Supply Chain Intern", "Austin, TX")
    assert a == b


def test_legal_suffix_variation_still_matches():
    # "Acme Corp" vs "Acme Corporation" vs "Acme, Inc." are the same
    # company as far as dedup should be concerned.
    a = compute_dedup_key("Acme Corp", "Supply Chain Intern", "Austin, TX")
    b = compute_dedup_key("Acme Corporation", "Supply Chain Intern", "Austin, TX")
    c = compute_dedup_key("Acme, Inc.", "Supply Chain Intern", "Austin, TX")
    assert a == b == c


def test_case_and_punctuation_do_not_matter():
    a = compute_dedup_key("Acme Corp", "Supply Chain Intern", "Austin, TX")
    b = compute_dedup_key("ACME CORP", "supply-chain intern!", "austin, tx")
    assert a == b


def test_slug_style_company_name_matches_properly_spaced_display_name():
    # Regression: confirmed live -- "GE Aerospace" (a hand-typed source)
    # and "geaerospace" (a raw Greenhouse/Workday URL slug, before this
    # project started giving those real display names) normalized to
    # different keys ("ge aerospace" vs "geaerospace") purely because of
    # the missing space, so two real duplicate postings sat 'open' side
    # by side and the sweep never caught it.
    a = compute_dedup_key("GE Aerospace", "Manufacturing Intern", "Singapore")
    b = compute_dedup_key("geaerospace", "Manufacturing Intern", "Singapore")
    assert a == b


def test_different_title_produces_different_key():
    a = compute_dedup_key("Acme Corp", "Supply Chain Intern", "Austin, TX")
    b = compute_dedup_key("Acme Corp", "Logistics Intern", "Austin, TX")
    assert a != b


def test_different_location_produces_different_key():
    a = compute_dedup_key("Acme Corp", "Supply Chain Intern", "Austin, TX")
    b = compute_dedup_key("Acme Corp", "Supply Chain Intern", "Dallas, TX")
    assert a != b


def test_missing_company_or_title_yields_no_key():
    assert compute_dedup_key("", "Supply Chain Intern", "Austin, TX") is None
    assert compute_dedup_key("Acme Corp", "", "Austin, TX") is None


def test_missing_location_still_yields_a_key():
    # location alone being blank shouldn't block dedup -- some sources
    # (e.g. USAJobs entries with no PositionLocationDisplay) legitimately
    # have an empty location string.
    assert compute_dedup_key("Acme Corp", "Supply Chain Intern", "") is not None


pytestmark_db = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ, reason="needs a scratch Postgres via DATABASE_URL"
)
