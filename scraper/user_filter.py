"""Applies a user's own filter spec (filters.yaml or a personal copy) to
the raw posting store. This is the customization point: fork the repo,
copy filters.yaml, edit the lists, run build_feed.py against your copy —
no scraper code to touch, no re-scraping needed, since data/all_postings.json
already holds every internship-shaped posting from every configured source.
"""
import re

import yaml


def load_filter(path: str) -> dict:
    with open(path) as f:
        spec = yaml.safe_load(f) or {}
    spec.setdefault("keywords_any", [])
    spec.setdefault("exclude_keywords", [])
    spec.setdefault("trusted_companies", [])
    return spec


def _matches_any(text: str, keywords: list[str]) -> bool:
    return any(re.search(r"\b" + re.escape(kw) + r"\b", text, re.IGNORECASE) for kw in keywords)


def passes(posting: dict, spec: dict) -> bool:
    """A posting passes if:
    - its company is in `trusted_companies` (a company whose whole business
      already is the target domain — its postings often don't repeat the
      domain's own keywords in their own text, e.g. a freight broker's
      "Internship 2026" never says "logistics"), UNLESS it also matches an
      `exclude_keywords` term; OR
    - its title+description matches a `keywords_any` term and no
      `exclude_keywords` term.
    """
    text = f"{posting.get('title', '')} {posting.get('description_snippet', '')}"
    if spec["exclude_keywords"] and _matches_any(text, spec["exclude_keywords"]):
        return False
    if posting.get("company") in spec["trusted_companies"]:
        return True
    return bool(spec["keywords_any"]) and _matches_any(text, spec["keywords_any"])


def apply_filter(postings: list[dict], spec: dict) -> list[dict]:
    return [p for p in postings if passes(p, spec)]
