import re

INTERN_KEYWORDS = [
    "intern",
    "internship",
    "co-op",
    "coop",
    "summer analyst",
]


def _matches_any(text: str, keywords: list[str]) -> bool:
    # \b word-boundary matching: plain substring search would match "intern"
    # inside "internal"/"international", and both show up constantly in
    # ordinary job-description boilerplate — confirmed live while building
    # this (Samsara's whole board matched until this was word-bounded).
    return any(re.search(r"\b" + re.escape(kw) + r"\b", text, re.IGNORECASE) for kw in keywords)


def is_internship(title: str) -> bool:
    """True if a posting's TITLE looks like an internship/co-op track role.

    This is the only filter scrape.py applies before writing to the raw
    store (data/all_postings.json) — deliberately domain-agnostic. What
    counts as "relevant" beyond "is this an internship at all" is a matter
    of the viewer's own interest, not something this project should decide
    once and bake into everyone's data — see scraper/user_filter.py and
    filters.yaml for where that judgment actually lives, as something any
    fork can change without touching scraper code.

    Runs on the title only, not the description: a description's prose can
    mention "intern" in an unrelated sentence ("mentor our interns",
    "reports internally") and false-positive a full-time role, whereas ATS
    titles reliably say "Intern"/"Internship" for internship-track roles.
    """
    return _matches_any(title, INTERN_KEYWORDS)
