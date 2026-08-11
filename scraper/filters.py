import re

INTERN_KEYWORDS = [
    "intern",
    "internship",
    "co-op",
    "coop",
    "summer analyst",
]

DOMAIN_KEYWORDS = [
    "supply chain",
    "logistics",
    "operations",
    "procurement",
    "sourcing",
    "warehouse",
    "distribution",
    "inventory",
    "fulfillment",
    "manufacturing operations",
    "transportation",
    "freight",
]

def _matches_any(text: str, keywords: list[str]) -> bool:
    # \b word-boundary matching: plain substring search would match "intern"
    # inside "internal"/"international", and both show up constantly in
    # ordinary job-description boilerplate — confirmed live while building
    # this (Samsara's whole board matched until this was word-bounded).
    return any(re.search(r"\b" + re.escape(kw) + r"\b", text, re.IGNORECASE) for kw in keywords)


def is_relevant(title: str, description_snippet: str = "", domain_native: bool = False) -> bool:
    """True if a posting looks like an ops/logistics/supply-chain internship.

    The intern-track check runs on the TITLE only — ATS titles reliably say
    "Intern"/"Internship" for internship roles, whereas a description's
    prose can mention "intern" in an unrelated sentence ("mentor our
    interns", "reports internally") and false-positive a full-time role.
    The domain check runs on title + description, and is skipped entirely
    when the source is marked `domain_native` in sources.yaml — a company
    whose entire business already is logistics/supply chain (e.g. a freight
    broker) posts internships that never say "supply chain" in the title,
    so gating those on a keyword match would silently drop them.
    """
    if not _matches_any(title, INTERN_KEYWORDS):
        return False
    if domain_native:
        return True
    text = f"{title} {description_snippet}"
    return _matches_any(text, DOMAIN_KEYWORDS)
