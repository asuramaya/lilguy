import requests

import sys
from pathlib import Path

from .base import Connector, Posting
from .util import to_display_text

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))
from work_arrangement import HYBRID, REMOTE, normalise  # noqa: E402

API = "https://api.ashbyhq.com/posting-api/job-board/{token}"


class AshbyConnector(Connector):
    """Ashby's public job-board API. No auth, no key.

    entry needs: {ats: ashby, company: "Display Name", token: "ashby-board-slug", category: "..."}
    The token is the slug in the company's own board URL:
    jobs.ashbyhq.com/<token>

    Worth knowing: the LIST response already carries descriptionPlain and
    descriptionHtml, so descriptions arrive free. That is not a small
    detail -- Workday's list endpoint carries none, which forces a
    per-posting fetch, which is where the description backfill deadlock
    (service/workday_descriptions.py) came from. A connector that needs
    no N+1 fetch cannot develop that class of problem.
    """

    name = "ashby"

    def fetch(self, entry: dict) -> list[Posting]:
        token = entry.get("token")
        if not token:
            raise ValueError(f"ashby entry for {entry.get('company')} is missing 'token'")

        resp = requests.get(API.format(token=token), timeout=20)
        if resp.status_code == 404:
            raise ValueError(
                f"ashby token '{token}' for {entry.get('company')} returned 404 — "
                "the token is wrong or the board doesn't exist"
            )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or "jobs" not in data:
            raise ValueError(f"unexpected ashby response shape for token '{token}': {type(data)}")

        postings = []
        for job in data["jobs"]:
            # isListed false means the board itself is hiding it -- an
            # unlisted job is reachable by direct link but is not being
            # advertised, and surfacing it would present something as
            # open that the employer has chosen not to show.
            if not job.get("isListed", True):
                continue

            job_id = job.get("id")
            if not job_id:
                continue

            plain = job.get("descriptionPlain") or ""
            # workplaceType is the richer field ("Hybrid", "Onsite",
            # "Remote"); isRemote is a boolean that cannot express
            # hybrid. Prefer the former and fall back to the latter,
            # rather than letting a hybrid role read as remote.
            arrangement = normalise(job.get("workplaceType"))
            if not arrangement and job.get("isRemote"):
                arrangement = REMOTE
            postings.append(
                Posting(
                    id=f"ashby:{token}:{job_id}",
                    company=entry.get("company", token),
                    title=job.get("title", "").strip(),
                    location=job.get("location", ""),
                    url=job.get("jobUrl") or job.get("applyUrl", ""),
                    source="ashby",
                    category=entry.get("category", ""),
                    work_arrangement=arrangement,
                    posted_at=job.get("publishedAt"),
                    description_snippet=plain[:600],
                    description=to_display_text(job.get("descriptionHtml") or plain),
                )
            )
        return postings
