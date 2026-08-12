import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from user_filter import passes  # noqa: E402

SPEC = {
    "keywords_any": ["supply chain", "logistics", "warehouse"],
    "exclude_keywords": ["hospitality"],
    "trusted_companies": ["Uber Freight"],
}


def posting(title="", description_snippet="", company="", location="", first_seen=None):
    return {
        "title": title,
        "description_snippet": description_snippet,
        "company": company,
        "location": location,
        "first_seen": first_seen,
    }


def test_keyword_match_in_title_passes():
    assert passes(posting(title="Supply Chain Intern"), SPEC)


def test_keyword_match_in_description_passes():
    assert passes(posting(title="Intern", description_snippet="Join our warehouse team"), SPEC)


def test_no_keyword_match_fails():
    assert not passes(posting(title="Marketing Intern"), SPEC)


def test_trusted_company_bypasses_keyword_check():
    # A freight broker's own internship posting often doesn't repeat
    # "logistics" in its own text — this is the whole reason
    # trusted_companies exists.
    assert passes(posting(title="Internship 2026", company="Uber Freight"), SPEC)


def test_exclude_keyword_overrides_keyword_match():
    assert not passes(
        posting(title="Hospitality Operations Intern", description_snippet="warehouse"), SPEC
    )


def test_exclude_keyword_overrides_trusted_company():
    # exclude_keywords is an escape hatch that should win even for a
    # trusted company — otherwise there's no way to filter out a bad
    # posting from an otherwise-trusted source.
    assert not passes(
        posting(title="Hospitality Intern", company="Uber Freight"), SPEC
    )


def test_word_boundary_matching_not_substring():
    # "warehouse" should not match "warehousing solutions provider" being
    # absent — but SHOULD match when the whole word appears; this checks
    # that a keyword like "logistics" doesn't accidentally match something
    # like "logisticsx" or get skipped due to a boundary bug.
    assert passes(posting(title="Logistics Intern"), SPEC)
    assert not passes(posting(title="Analytics Intern", description_snippet="no relevant terms here"), SPEC)


def test_empty_spec_matches_nothing_by_default():
    empty = {"keywords_any": [], "exclude_keywords": [], "trusted_companies": []}
    assert not passes(posting(title="Supply Chain Intern"), empty)


def test_singular_vs_plural_are_distinct_keywords():
    # Regression: a real Unilever posting titled "Logistic Intern"
    # (singular) didn't match a keywords_any list containing only
    # "logistics" (plural) — word-boundary matching doesn't stem.
    only_plural = {"keywords_any": ["logistics"], "exclude_keywords": [], "trusted_companies": []}
    assert not passes(posting(title="Logistic Intern"), only_plural)
    both_forms = {"keywords_any": ["logistics", "logistic"], "exclude_keywords": [], "trusted_companies": []}
    assert passes(posting(title="Logistic Intern"), both_forms)
    assert passes(posting(title="Logistics Intern"), both_forms)


def test_missing_optional_fields_do_not_crash():
    # A hand-built spec dict (not run through load_filter()'s setdefault
    # calls) shouldn't KeyError just because it skipped an optional field.
    minimal = {"keywords_any": ["logistics"]}
    assert passes(posting(title="Logistics Intern"), minimal)


def test_locations_include_filters_by_location():
    spec = {**SPEC, "locations_include": ["United States"]}
    assert passes(posting(title="Supply Chain Intern", location="Austin, United States"), spec)
    assert not passes(posting(title="Supply Chain Intern", location="Berlin, Germany"), spec)


def test_locations_include_word_boundary_not_substring():
    # A plain substring check on "US" would match inside "Australia" —
    # this must not.
    spec = {**SPEC, "locations_include": ["US"]}
    assert not passes(posting(title="Supply Chain Intern", location="Sydney, Australia"), spec)
    assert passes(posting(title="Supply Chain Intern", location="Austin, US"), spec)


def test_locations_exclude_overrides_everything_else():
    spec = {**SPEC, "locations_exclude": ["India"]}
    assert not passes(
        posting(title="Internship 2026", company="Uber Freight", location="Bangalore, India"), spec
    )


def test_trusted_company_still_subject_to_locations_include():
    # A location filter is a genuine additional gate, not an alternate
    # way to pass — a trusted company's posting still has to be in the
    # right place if a location filter is set at all.
    spec = {**SPEC, "locations_include": ["United States"]}
    assert not passes(
        posting(title="Internship 2026", company="Uber Freight", location="Berlin, Germany"), spec
    )
    assert passes(
        posting(title="Internship 2026", company="Uber Freight", location="Austin, United States"), spec
    )


def test_max_age_days_drops_old_postings():
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    spec = {**SPEC, "max_age_days": 14}
    fresh = posting(title="Logistics Intern", first_seen=(now - timedelta(days=2)).isoformat())
    stale = posting(title="Logistics Intern", first_seen=(now - timedelta(days=30)).isoformat())
    assert passes(fresh, spec, now=now)
    assert not passes(stale, spec, now=now)


def test_max_age_days_none_means_no_filtering():
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    ancient = posting(title="Logistics Intern", first_seen="2020-01-01T00:00:00+00:00")
    assert passes(ancient, SPEC, now=now)


def test_missing_first_seen_is_not_penalized_by_max_age():
    # Can't judge an age we don't have — don't drop a posting just
    # because first_seen is missing/unparseable.
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    spec = {**SPEC, "max_age_days": 14}
    no_date = posting(title="Logistics Intern", first_seen=None)
    assert passes(no_date, spec, now=now)
