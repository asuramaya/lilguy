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


def fetch_missing_descriptions(limit: int = 10, pace: float = None, session=None) -> dict:
    kwargs = {"session": session}
    if pace is not None:
        kwargs["pace"] = pace
    return run(WorkdayDescriptions(), limit=limit, **kwargs)
