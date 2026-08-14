import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

from analyze_discovery_evidence import summarize  # noqa: E402


def test_counts_by_review_status():
    rows = [
        {"review_status": "no_match", "evidence": None, "ats": None},
        {"review_status": "no_match", "evidence": None, "ats": None},
        {"review_status": "promoted", "evidence": {"name_similarity": 0.9, "intern_count": 3}, "ats": "greenhouse"},
        {"review_status": "rejected", "evidence": {"reason": "zero postings returned"}, "ats": "workday"},
    ]
    result = summarize(rows)
    assert result["counts_by_review_status"] == {"no_match": 2, "promoted": 1, "rejected": 1}


def test_rejection_reasons_are_tallied():
    rows = [
        {"review_status": "rejected", "evidence": {"reason": "zero postings returned"}, "ats": "workday"},
        {"review_status": "rejected", "evidence": {"reason": "zero postings returned"}, "ats": "workday"},
        {"review_status": "rejected", "evidence": {"reason": "no internship-shaped titles found"}, "ats": "jsonld"},
    ]
    result = summarize(rows)
    assert result["rejected_reasons"] == {"zero postings returned": 2, "no internship-shaped titles found": 1}


def test_name_similarity_stats_split_by_outcome():
    rows = [
        {"review_status": "promoted", "evidence": {"name_similarity": 0.9}, "ats": "greenhouse"},
        {"review_status": "promoted", "evidence": {"name_similarity": 1.0}, "ats": "greenhouse"},
        {"review_status": "rejected", "evidence": {"name_similarity": 0.2}, "ats": "workday"},
    ]
    result = summarize(rows)
    assert result["name_similarity"]["promoted"] == {"n": 2, "min": 0.9, "max": 1.0, "mean": 0.95, "median": 0.95}
    assert result["name_similarity"]["rejected"]["n"] == 1


def test_missing_evidence_does_not_crash():
    rows = [{"review_status": "no_match", "evidence": None, "ats": None}]
    result = summarize(rows)
    assert result["counts_by_review_status"] == {"no_match": 1}
    assert result["rejected_reasons"] == {}


def test_evidence_without_name_similarity_key_is_skipped_not_crashed():
    # e.g. a 'rejected' outcome from "trial fetch raised" never got as
    # far as verify_trial_fetch() at all, so its evidence dict is empty.
    rows = [{"review_status": "rejected", "evidence": {}, "ats": "greenhouse"}]
    result = summarize(rows)
    assert result["name_similarity"]["rejected"] == {"n": 0}
