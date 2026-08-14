"""The automated verification gate a discovery hit must pass before it's
trusted enough to go on probation (see scheduler.py's promotion logic for
the second half of the two-strike check).

This exists because "the probe returned HTTP 200 with parseable JSON" is
NOT the same fact as "this is really the company's internship board" --
proven live, by hand, multiple times in one session: a wrong Workday
tenant/site guess 404s or 422s cleanly (that's easy to catch), but a
RIGHT-looking tenant string can also belong to an unrelated company, an
empty-but-valid board, or a page that parses fine but isn't job postings
at all. Each check below encodes one specific way that already went
wrong, made automatic instead of relying on a human re-reading the
output.
"""
import re
from difflib import SequenceMatcher

from filters import is_internship  # noqa: E402  (scraper/ on sys.path via discovery.py)

MIN_INTERNSHIP_POSTINGS = 1
MIN_DISTINCT_TITLES = 2
NAME_MATCH_THRESHOLD = 0.5


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _name_similarity(candidate_company: str, fetched_company_names: list) -> float:
    """Best fuzzy-match ratio between the candidate name we were probing
    FOR and any company name the fetch actually returned. Guards against
    a Workday tenant/site guess that resolves cleanly but belongs to a
    different company than the one being searched for -- a real failure
    mode with tenant-string collisions, not a hypothetical one."""
    cand = _normalize(candidate_company)
    if not fetched_company_names:
        return 0.0
    return max(SequenceMatcher(None, cand, _normalize(name)).ratio() for name in fetched_company_names)


def verify_trial_fetch(candidate_company: str, postings: list) -> dict:
    """postings: list[Posting] from a real connector.fetch() trial run
    (NOT filtered to internship-shaped titles yet -- this function does
    that itself, the same way scheduler.py's run_one() does for a
    confirmed source).

    Returns {"passed": bool, "reason": str, "evidence": {...}} -- reason
    and evidence are for discovery_candidates.evidence, so a rejection is
    legible later without re-running the probe.
    """
    total = len(postings)
    intern_postings = [p for p in postings if is_internship(p.title)]
    distinct_titles = len({p.title for p in postings})
    company_names = [p.company for p in postings if p.company]

    evidence = {
        "total": total,
        "intern_count": len(intern_postings),
        "distinct_titles": distinct_titles,
        "sample_titles": [p.title for p in intern_postings[:5]] or [p.title for p in postings[:5]],
    }

    if total == 0:
        return {"passed": False, "reason": "zero postings returned", "evidence": evidence}

    if len(intern_postings) < MIN_INTERNSHIP_POSTINGS:
        return {"passed": False, "reason": "no internship-shaped titles found", "evidence": evidence}

    if total >= MIN_DISTINCT_TITLES and distinct_titles < MIN_DISTINCT_TITLES:
        # Every posting has (almost) the same title -- looks more like a
        # generic error/placeholder page that happened to parse than a
        # real job board with multiple distinct openings.
        return {"passed": False, "reason": "postings aren't distinct (looks like a placeholder page)",
                "evidence": evidence}

    similarity = _name_similarity(candidate_company, company_names)
    evidence["name_similarity"] = round(similarity, 2)
    if similarity < NAME_MATCH_THRESHOLD:
        return {"passed": False,
                "reason": f"fetched company name doesn't resemble '{candidate_company}' "
                          f"(best match {similarity:.2f}, need >= {NAME_MATCH_THRESHOLD}) -- "
                          "possible tenant/site collision with a different company",
                "evidence": evidence}

    return {"passed": True, "reason": "ok", "evidence": evidence}
