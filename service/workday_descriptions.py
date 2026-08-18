"""Backfills descriptions for Workday postings.

Every other direct connector hands back the full description inside the
list response we already fetch -- Greenhouse's `content`, Muse's
`contents`, Lever's `descriptionPlain`, JSON-LD's `description`,
Oracle's three composable fields, Ashby's `descriptionHtml`. Workday's
job-search endpoint carries none: the closest thing, `bulletFields[0]`,
is a requisition-ID stub. So the text has to come from Workday's
per-posting CXS endpoint, confirmed live:

    GET https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{external_path}
    -> {"jobPostingInfo": {"jobDescription": "<html>", ...}, ...}

The QUEUE mechanics -- claiming, backoff, terminal-vs-transient
classification, the deliberately narrow try/except -- now live in
service/description_backfill.py. This module supplies only what is
genuinely Workday-specific. That split exists because SmartRecruiters
needs the identical drain and two copies would drift; this is also the
module where the head-of-line deadlock happened, so there is real value
in there being exactly one implementation of it.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from connectors.util import to_display_text  # noqa: E402
from description_backfill import (  # noqa: E402
    BACKOFF_HOURS,
    TERMINAL_STATUSES,
    DescriptionSource,
    run,
)

DETAIL_URL = "https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{external_path}"

# A Common-Crawl-discovered Workday tenant is seeded with company=tenant
# (see discovery.py's `config = {"company": t["tenant"], ...}`) because
# nothing at discovery time knows the real name -- Morgan Stanley's own
# tenant is the opaque "ms". The per-posting detail payload this module
# already fetches for its description carries a real one in
# `hiringOrganization.name`, sitting unused. Confirmed live against the
# "ms" tenant: `hiringOrganization.name` is "711 MS Smith Barney, LLC" --
# a legal-entity name, not the brand, but "711 MS Smith Barney, LLC" is
# still a dramatically more useful company column than the bare string
# "ms", and stripping the common corporate suffixes below is as far as
# this goes deliberately -- guessing at a shortened "brand" name from an
# arbitrary legal name is exactly the kind of over-fitting that reads
# fine on the one example you tested and breaks on the next.
_LEGAL_SUFFIX_RE = re.compile(
    r",?\s+(L\.?L\.?C\.?|Inc\.?|Incorporated|Corp\.?|Corporation|Ltd\.?|Limited|PLC|Co\.?)\.?$",
    re.IGNORECASE,
)


def _clean_company_name(name: str) -> str:
    name = (name or "").strip()
    # Looped rather than applied once: a name can carry more than one
    # suffix ("Foo Corp, Inc." -> "Foo Corp" -> "Foo"), and re.sub only
    # strips whichever one happens to sit at the very end per pass.
    stripped = _LEGAL_SUFFIX_RE.sub("", name).strip()
    while stripped != name:
        name, stripped = stripped, _LEGAL_SUFFIX_RE.sub("", stripped).strip()
    return name

# Re-exported so existing callers and tests keep their import site.
__all__ = ["fetch_missing_descriptions", "BACKOFF_HOURS", "TERMINAL_STATUSES", "DETAIL_URL"]


class WorkdayDescriptions(DescriptionSource):
    ats = "workday"
    config_fields = ("tenant", "wd_host", "site")

    def build_url(self, row):
        # Posting ids are built as f"workday:{tenant}:{site}:{path}" by
        # scraper/connectors/workday.py, and the path itself contains
        # slashes and can contain colons -- so the split is bounded at 3
        # rather than greedy, keeping the whole remainder as the path.
        parts = (row["id"] or "").split(":", 3)
        if len(parts) != 4 or not parts[3]:
            return None
        if not all(row.get(field) for field in self.config_fields):
            return None
        return DETAIL_URL.format(
            tenant=row["tenant"], wd_host=row["wd_host"],
            site=row["site"], external_path=parts[3],
        )

    def extract_text(self, payload: dict) -> str:
        return to_display_text((payload.get("jobPostingInfo") or {}).get("jobDescription") or "")

    def maybe_fix_company(self, row: dict, payload: dict) -> str | None:
        # Only when the source's OWN company still equals its tenant --
        # a strong, specific signal that it was never resolved past the
        # discovery-time placeholder. A source with any real name already
        # set (the overwhelming majority -- most Workday sources come
        # from sources.yaml with a real company from day one) is left
        # alone even if this happens to differ, since a hand-entered or
        # already-corrected name outranks a guess from one job's detail
        # page.
        tenant = (row.get("tenant") or "").strip()
        current = (row.get("source_company") or "").strip()
        if not tenant or current.lower() != tenant.lower():
            return None
        name = _clean_company_name((payload.get("hiringOrganization") or {}).get("name") or "")
        if not name or name.lower() == tenant.lower():
            return None
        return name


def fetch_missing_descriptions(limit: int = 10, pace: float = None, session=None) -> dict:
    kwargs = {"session": session}
    if pace is not None:
        kwargs["pace"] = pace
    return run(WorkdayDescriptions(), limit=limit, **kwargs)
