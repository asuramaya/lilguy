import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from filters import is_internship  # noqa: E402


def test_plain_intern_title():
    assert is_internship("Supply Chain Intern")


def test_internship_word():
    assert is_internship("Warehouse Operations Internship")


def test_co_op():
    assert is_internship("Logistics Co-op")


def test_full_time_role_is_not_an_internship():
    assert not is_internship("Warehouse Operations Manager")


def test_word_boundary_does_not_match_substring():
    # Regression: plain substring search matched "intern" inside
    # "internal"/"international" and once flagged an entire company's job
    # board as internships. See docs/sourcing-model.md.
    assert not is_internship("Internal Audit Manager")
    assert not is_internship("International Logistics Director")


def test_case_insensitive():
    assert is_internship("SUPPLY CHAIN INTERN")
    assert is_internship("supply chain intern")
