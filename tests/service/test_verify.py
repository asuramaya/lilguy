import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

from verify import verify_trial_fetch  # noqa: E402


def fake_posting(company, title):
    return SimpleNamespace(company=company, title=title)


def test_zero_postings_fails():
    v = verify_trial_fetch("Acme Corp", [])
    assert not v["passed"]
    assert "zero postings" in v["reason"]


def test_no_internship_shaped_titles_fails():
    postings = [fake_posting("Acme Corp", "Senior Software Engineer"),
                fake_posting("Acme Corp", "Sales Director")]
    v = verify_trial_fetch("Acme Corp", postings)
    assert not v["passed"]
    assert "internship" in v["reason"]


def test_identical_titles_fails_as_placeholder_looking():
    postings = [fake_posting("Acme Corp", "Intern") for _ in range(5)]
    v = verify_trial_fetch("Acme Corp", postings)
    assert not v["passed"]
    assert "distinct" in v["reason"]


def test_company_name_mismatch_fails():
    # Simulates a Workday tenant/site guess that resolves cleanly but
    # belongs to an unrelated company -- a real failure mode, not
    # hypothetical (see verify.py's own module docstring).
    postings = [fake_posting("Totally Different Inc", "Marketing Intern"),
                fake_posting("Totally Different Inc", "Finance Intern")]
    v = verify_trial_fetch("Acme Corp", postings)
    assert not v["passed"]
    assert "collision" in v["reason"]


def test_real_looking_result_passes():
    postings = [
        fake_posting("Acme Corp", "Supply Chain Intern"),
        fake_posting("Acme Corp", "Software Engineer II"),
        fake_posting("Acme Corp", "Logistics Intern - Summer 2027"),
    ]
    v = verify_trial_fetch("Acme Corp", postings)
    assert v["passed"]
    assert v["evidence"]["intern_count"] == 2


def test_slightly_different_company_string_still_passes():
    # "Acme Corp" vs "Acme Corporation" -- the fuzzy match should
    # tolerate a legal-suffix difference, not just exact strings.
    postings = [fake_posting("Acme Corporation", "Operations Intern")]
    v = verify_trial_fetch("Acme Corp", postings)
    assert v["passed"]
