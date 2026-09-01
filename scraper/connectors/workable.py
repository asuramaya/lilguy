import time

import requests

from .base import Connector, Posting
from .util import strip_html, to_display_text

# v3's own /jobs search endpoint, not the v1 "?full=true" account
# endpoint -- confirmed live BOTH ways this session: v3 answers a bare,
# cookie-free request (no browser session) with a clean 200/404 signal
# per token; v1 "?full=true" answered 200 with real data ONLY from
# inside an actual browser tab that had first loaded the board page (and
# so carried its session/WAF cookies) -- the exact same request replayed
# standalone came back 404 even for a real, live token. v3 is the one
# that actually works as a plain server-to-server call.
LIST_API = "https://apply.workable.com/api/v3/accounts/{token}/jobs"
# Per-job detail, for description text -- v3's list response doesn't
# carry it (same shape as Workday's list-lacks-description gap, see
# workday.py). NOT confirmed live this session (hit Workable's rate
# limit mid-investigation before reaching a real posting to check
# against) -- if this consistently 404s or comes back empty once running
# in production, that's the first thing to re-verify, not a sign the
# rest of this connector is wrong.
DETAIL_API = "https://apply.workable.com/api/v3/accounts/{token}/jobs/{shortcode}"

# Confirmed live: firing several of these back-to-back with no pacing
# gets 429'd hard (a matter of ~10 requests in quick succession, not
# hundreds) -- same shape as jsonld.py's RTX finding, but tighter here.
REQUEST_DELAY_SECONDS = 1.0


class WorkableConnector(Connector):
    """Workable's public candidate-facing job-board API. No auth required,
    but Workable's WAF 403s a request with no Referer header even for a
    perfectly valid token -- confirmed live: the exact same request
    against the exact same account went from 403 to 200 by adding
    `Referer: https://apply.workable.com/<token>/` alone, nothing else
    changed. Every request below sends it.

    entry needs: {ats: workable, company: "Display Name", token: "account-slug", category: "..."}
    The token is the slug in the company's own board URL:
    apply.workable.com/<token>/
    """

    name = "workable"

    def _headers(self, token: str) -> dict:
        return {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Referer": f"https://apply.workable.com/{token}/",
            "Content-Type": "application/json",
        }

    def fetch(self, entry: dict) -> list[Posting]:
        token = entry.get("token")
        if not token:
            raise ValueError(f"workable entry for {entry.get('company')} is missing 'token'")

        resp = requests.post(LIST_API.format(token=token), json={}, headers=self._headers(token), timeout=20)
        if resp.status_code == 404:
            raise ValueError(
                f"workable token '{token}' for {entry.get('company')} returned 404 — "
                "the token is wrong or the board doesn't exist"
            )
        resp.raise_for_status()
        data = resp.json()
        if "results" not in data:
            raise ValueError(f"unexpected workable response shape for token '{token}': {list(data.keys())}")

        postings = []
        for job in data["results"]:
            shortcode = job.get("shortcode") or job.get("id")
            title = (job.get("title") or "").strip()
            if not shortcode or not title:
                continue

            loc_parts = [job.get("city"), job.get("state"), job.get("country")]
            location = ", ".join(p for p in loc_parts if p) or "Not specified"
            if not any(loc_parts) and job.get("telecommuting"):
                location = "Remote"

            job_url = job.get("url") or job.get("application_url") or job.get("shortlink") or \
                f"https://apply.workable.com/{token}/j/{shortcode}/"

            description_raw = self._fetch_description(token, shortcode)

            postings.append(
                Posting(
                    id=f"workable:{token}:{shortcode}",
                    company=entry.get("company", token),
                    title=title,
                    location=location,
                    url=job_url,
                    source="workable",
                    category=entry.get("category", ""),
                    posted_at=job.get("published_on") or job.get("created_at"),
                    description_snippet=strip_html(description_raw),
                    description=to_display_text(description_raw),
                )
            )
        return postings

    def _fetch_description(self, token: str, shortcode: str) -> str:
        # A dead detail endpoint (see DETAIL_API's own note) shouldn't
        # sink the whole posting -- same tolerance as jsonld.py's
        # per-page fetch failures. Worst case: a real posting with a
        # blank description, not a missing posting.
        try:
            resp = requests.get(DETAIL_API.format(token=token, shortcode=shortcode),
                                 headers=self._headers(token), timeout=15)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code != 200:
                return ""
            return (resp.json() or {}).get("description", "") or ""
        except Exception:  # noqa: BLE001
            return ""
