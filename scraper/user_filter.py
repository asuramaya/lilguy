"""Applies a user's own filter spec (filters.yaml or a personal copy) to
the raw posting store. This is the customization point: fork the repo,
copy filters.yaml, edit the lists, run build_feed.py against your copy —
no scraper code to touch, no re-scraping needed, since data/all_postings.json
already holds every internship-shaped posting from every configured source.
"""
import re
from datetime import datetime, timedelta, timezone

import yaml


def load_filter(path: str) -> dict:
    with open(path) as f:
        spec = yaml.safe_load(f) or {}
    spec.setdefault("keywords_any", [])
    spec.setdefault("exclude_keywords", [])
    spec.setdefault("trusted_companies", [])
    spec.setdefault("locations_include", [])
    spec.setdefault("locations_exclude", [])
    spec.setdefault("max_age_days", None)
    return spec


def _matches_any(text: str, keywords: list[str]) -> bool:
    # Word-boundary, not stemmed: "logistic" and "logistics" are two
    # different keywords as far as this is concerned. Confirmed live that
    # this matters — a real Unilever posting titled "Logistic Intern"
    # (singular) didn't match a keywords_any list that only had
    # "logistics" (plural). If a keyword you'd expect to match isn't
    # catching something, check for a singular/plural or verb-tense
    # variant before assuming the posting's text doesn't mention it.
    # Word-boundary matters just as much for locations as keywords — a
    # plain substring check on "US" would match inside "ustralia"
    # (Australia), so locations_include/exclude use this same helper
    # rather than a separate, looser check.
    return any(re.search(r"\b" + re.escape(kw) + r"\b", text, re.IGNORECASE) for kw in keywords)


def _too_old(posting: dict, max_age_days, now: datetime) -> bool:
    if not max_age_days:
        return False
    first_seen = posting.get("first_seen")
    if not first_seen:
        return False  # can't judge age we don't have — don't punish the posting for it
    try:
        seen_at = datetime.fromisoformat(first_seen)
    except ValueError:
        return False
    if seen_at.tzinfo is None:
        seen_at = seen_at.replace(tzinfo=timezone.utc)
    return (now - seen_at) > timedelta(days=max_age_days)


def passes(posting: dict, spec: dict, now: datetime = None) -> bool:
    """A posting passes if ALL of:
    - it isn't vetoed by exclude_keywords, locations_exclude, or
      max_age_days (based on OUR OWN first_seen — see the note below on
      why not each source's own `posted_at`);
    - EITHER its company is in `trusted_companies` (a company whose whole
      business already is the target domain — its postings often don't
      repeat the domain's own keywords in their own text, e.g. a freight
      broker's "Internship 2026" never says "logistics") OR its
      title+description matches a `keywords_any` term;
    - AND, if `locations_include` is set, its location matches at least
      one entry (this is a genuine additional filter, not an alternate
      way to pass — a trusted company's posting still has to be in the
      right place if you've set a location filter at all).

    `max_age_days` is checked against `first_seen` (when THIS project's
    scraper first recorded the posting), not each source's own
    `posted_at` — deliberately, because `posted_at` format varies wildly
    across sources (an ISO date from Greenhouse, a relative string like
    "Posted 15 Days Ago" from some Workday tenants) and isn't reliably
    parseable as one thing. `first_seen` is always the scraper's own ISO
    timestamp, so it's the one field guaranteed comparable across every
    source.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    text = f"{posting.get('title', '')} {posting.get('description_snippet', '')}"
    location = posting.get("location", "")

    # .get() throughout, not spec[...] — a hand-built spec dict (as in
    # this project's own tests, or a caller that didn't go through
    # load_filter()'s setdefault calls) shouldn't KeyError just because it
    # didn't set every optional field.
    if spec.get("exclude_keywords") and _matches_any(text, spec["exclude_keywords"]):
        return False
    if spec.get("locations_exclude") and _matches_any(location, spec["locations_exclude"]):
        return False
    if _too_old(posting, spec.get("max_age_days"), now):
        return False

    is_trusted = posting.get("company") in spec.get("trusted_companies", [])
    keyword_match = bool(spec.get("keywords_any")) and _matches_any(text, spec["keywords_any"])
    if not (is_trusted or keyword_match):
        return False

    if spec.get("locations_include") and not _matches_any(location, spec["locations_include"]):
        return False

    return True


def apply_filter(postings: list[dict], spec: dict, now: datetime = None) -> list[dict]:
    return [p for p in postings if passes(p, spec, now)]
