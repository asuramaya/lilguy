"""Normalises where a job is actually done: remote, hybrid or onsite.

Two sources, and the difference between them is the whole design.

A platform's STRUCTURED field (Ashby's isRemote/workplaceType,
SmartRecruiters' location.remote/hybrid) is the employer answering the
question directly. That is authoritative.

A location string that literally reads "Remote" or "Hybrid - Chicago" is
the same answer written somewhere else. Reading it is not inference.

Description text is where this stops. Descriptions discuss remote policy
in ways that invert meaning -- "this role is not remote", "occasional
remote work permitted", "we are a remote-first company" on an onsite
role -- so scanning them produces confident wrong answers, which is
worse than a blank. Operator ruling, 2026-08-17: platform field plus
explicit location text, nothing else.

Blank means "no source said", exactly as it does for industry and
job function.
"""
import re

REMOTE = "remote"
HYBRID = "hybrid"
ONSITE = "onsite"

VALID = (REMOTE, HYBRID, ONSITE)

# Deliberately anchored to whole words. "Remote" inside a place name
# would be a false positive, and there are real places called Remote.
_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
_REMOTE_RE = re.compile(r"\b(remote|telework|work\s?from\s?home|wfh|virtual)\b", re.I)
# The separator is optional AND may be a space: employers write "On-site",
# "Onsite" and "In Person" interchangeably, and matching only the
# hyphenated forms missed the one people type most.
_ONSITE_RE = re.compile(r"\b(on[-\s]?site|in[-\s]?office|in[-\s]?person)\b", re.I)


def from_location(location: str) -> str:
    """Reads an arrangement out of a location string, or returns ''.

    HYBRID is checked before REMOTE because "Hybrid - Remote/Chicago"
    occurs and hybrid is the more specific claim; matching remote first
    would flatten a hybrid role into a remote one.
    """
    text = location or ""
    if _HYBRID_RE.search(text):
        return HYBRID
    if _REMOTE_RE.search(text):
        return REMOTE
    if _ONSITE_RE.search(text):
        return ONSITE
    return ""


def normalise(value: str) -> str:
    """Maps a platform's own vocabulary onto ours, or returns ''.

    Unknown values become blank rather than being passed through: a
    platform inventing a fourth word should leave the field empty, not
    quietly add a category that no filter offers and no reader expects.
    """
    v = (value or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    if v in ("remote", "fullyremote", "remotefirst"):
        return REMOTE
    if v == "hybrid":
        return HYBRID
    if v in ("onsite", "inoffice", "inperson", "office"):
        return ONSITE
    return ""
