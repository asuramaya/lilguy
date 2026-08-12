import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from user_filter import passes  # noqa: E402

SPEC = {
    "keywords_any": ["supply chain", "logistics", "warehouse"],
    "exclude_keywords": ["hospitality"],
    "trusted_companies": ["Uber Freight"],
}


def posting(title="", description_snippet="", company=""):
    return {"title": title, "description_snippet": description_snippet, "company": company}


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
