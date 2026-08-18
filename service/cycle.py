"""Extracts the hiring cycle (season + year) a posting advertises.

Internships are seasonal, and the cycle is the thing a student filters
on first: in August 2026 the live question is "what's open for Summer
2027", because that is when Summer 2027 recruiting happens. Nothing in
the corpus recorded it, so the only way to ask was to read every title.

Measured on the live corpus (4,190 open postings) before building this:
1,677 titles name a year and 897 name a season, so coverage is about
40% -- real, but far from total. Everything downstream has to say so;
a filter that silently hides the 60% with no stated cycle would repeat
the exact bug that splitting industry from job function fixed.

TESTED AND REJECTED: that a 2025 cycle in an August-2026 title means the
posting is dead. Sampled those URLs -- they return 200, still live on
their boards, and several are genuine "2025-2026" academic-year ranges.
Cycle is a filter, not a staleness signal.
"""
import re

# \b on both sides so a requisition number can't donate a year: in
# "JR002026" the digits are preceded by a word character, so there is no
# boundary and no match.
_YEAR_RE = re.compile(r"\b(20[2-3][0-9])\b")

# "FY27" and "'27". Deliberately narrow -- a bare two-digit number is far
# too common in titles ("Intern 27") to read as a year.
_SHORT_YEAR_RE = re.compile(r"\b(?:FY|fy)\s*'?([2-3][0-9])\b|(?<![\w])'([2-3][0-9])\b")

_SEASON_RE = re.compile(r"\b(summer|fall|autumn|winter|spring)\b", re.I)

# Autumn and fall are the same season under two names, and a reader
# filtering "fall" means both. Normalising at parse time keeps one
# spelling in the column rather than making every query remember two.
_SEASON_ALIASES = {"autumn": "fall"}


def parse_cycle(title: str) -> tuple[str, int | None]:
    """Returns (season, year); either half may be missing.

    A title can carry one without the other -- "2027 Start" names a year
    with no season, "Summer Internship" the reverse -- and both are
    useful on their own, so they are extracted independently rather than
    requiring the pair.

    FIRST match wins for each. Titles lead with the cycle they advertise,
    and for an academic-year range ("Fall/Winter 2026-2027", 13 postings
    in the corpus) the first year is when the programme starts. Likewise
    the first season of "Fall/Winter" (14 postings). Both forms are rare
    enough that carrying a start/end pair through the schema, the API and
    the UI would cost far more than it settles.
    """
    text = title or ""

    season = ""
    match = _SEASON_RE.search(text)
    if match:
        season = match.group(1).lower()
        season = _SEASON_ALIASES.get(season, season)

    year = None
    match = _YEAR_RE.search(text)
    if match:
        year = int(match.group(1))
    else:
        match = _SHORT_YEAR_RE.search(text)
        if match:
            year = 2000 + int(match.group(1) or match.group(2))

    return season, year
