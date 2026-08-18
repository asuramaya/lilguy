"""Fills descriptions for SmartRecruiters postings.

Its list endpoint carries none, so scraper/connectors/smartrecruiters.py
deliberately stores NULL rather than putting an N+1 detail fetch on the
scrape path. This is the drain that closes that gap.

All the queue mechanics -- backoff, total ordering, terminal-vs-transient
classification, the narrow try -- live in description_backfill.py, which
exists so this is not a second copy of the Workday drain waiting to
drift from it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from connectors.util import to_display_text  # noqa: E402
from description_backfill import DescriptionSource, run  # noqa: E402

DETAIL_URL = "https://api.smartrecruiters.com/v1/companies/{token}/postings/{job_id}"

# Order matters: this is the reading order a person expects on the page,
# not the order the API happens to return.
SECTIONS = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")


class SmartRecruitersDescriptions(DescriptionSource):
    ats = "smartrecruiters"
    config_fields = ("token",)

    def build_url(self, row):
        # Ids are built as f"smartrecruiters:{token}:{job_id}" by the
        # connector. Split from the LEFT with maxsplit=2 so a job id
        # containing a colon stays intact.
        parts = (row["id"] or "").split(":", 2)
        if len(parts) != 3 or not parts[2] or not row.get("token"):
            return None
        return DETAIL_URL.format(token=row["token"], job_id=parts[2])

    def extract_text(self, payload: dict) -> str:
        """Composes from whichever sections have content.

        Not just jobDescription: confirmed live that a real posting can
        have an EMPTY jobDescription while additionalInformation carries
        the only text there is. Taking one section and calling it the
        description would have stored '' for such a posting and stopped
        asking, permanently.
        """
        sections = ((payload.get("jobAd") or {}).get("sections") or {})
        parts = []
        for name in SECTIONS:
            body = sections.get(name) or {}
            text = to_display_text(body.get("text") or "")
            if text:
                title = (body.get("title") or "").strip()
                parts.append(f"{title}\n{text}" if title else text)
        return "\n\n".join(parts)


def fetch_missing_descriptions(limit: int = 10, pace: float = None, session=None) -> dict:
    kwargs = {"session": session}
    if pace is not None:
        kwargs["pace"] = pace
    return run(SmartRecruitersDescriptions(), limit=limit, **kwargs)
