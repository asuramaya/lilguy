import requests

from .base import Connector, Posting

API = "https://api.smartrecruiters.com/v1/companies/{token}/postings"
PUBLIC_URL = "https://jobs.smartrecruiters.com/{token}/{job_id}"
PAGE_SIZE = 100


class SmartRecruitersConnector(Connector):
    """SmartRecruiters' public postings API. No auth, no key.

    entry needs: {ats: smartrecruiters, company: "Display Name",
                  token: "SmartRecruitersCompanyId", category: "...", max_pages?}
    The token is the company identifier in the board URL:
    jobs.smartrecruiters.com/<token>. It is CASE-SENSITIVE ("Visa", not
    "visa") -- a wrong case returns an empty list rather than a 404, so a
    typo looks exactly like a company with no openings.

    Two things this connector deliberately does not do:

    NO DESCRIPTIONS. The list response carries none; they live behind a
    per-posting fetch of `ref`. Descriptions are therefore left unset,
    which stores NULL ("not fetched yet") rather than '' ("the source
    has none") -- the same three-state convention Workday relies on, so
    a future backfill can find these rows. Adding the N+1 fetch here
    would put it on the scrape path, which is exactly the shape that
    deadlocked the Workday backfill.

    NO INDUSTRY FROM THE PLATFORM. Each posting carries an `industry`
    label ("Information Technology And Services"), but that is
    SmartRecruiters' vocabulary, and writing it into `category` would
    reintroduce a third taxonomy into the column two of them were just
    disentangled from. The curated sources.yaml industry is used instead.
    The `function` label IS used, because job_function had no vocabulary
    of its own on direct boards at all -- this is the first source that
    reports one.
    """

    name = "smartrecruiters"

    def fetch(self, entry: dict) -> list[Posting]:
        token = entry.get("token")
        if not token:
            raise ValueError(f"smartrecruiters entry for {entry.get('company')} is missing 'token'")

        max_pages = int(entry.get("max_pages", 10))
        postings, offset = [], 0

        for _ in range(max_pages):
            resp = requests.get(
                API.format(token=token),
                params={"limit": PAGE_SIZE, "offset": offset},
                timeout=20,
            )
            if resp.status_code == 404:
                raise ValueError(
                    f"smartrecruiters token '{token}' for {entry.get('company')} returned 404 — "
                    "the token is wrong or the board doesn't exist"
                )
            resp.raise_for_status()
            data = resp.json()
            page = data.get("content")
            if page is None:
                raise ValueError(
                    f"unexpected smartrecruiters response shape for token '{token}': "
                    f"{sorted(data)[:6]}"
                )

            for job in page:
                job_id = job.get("id")
                if not job_id:
                    continue
                postings.append(
                    Posting(
                        id=f"smartrecruiters:{token}:{job_id}",
                        company=entry.get("company", token),
                        title=(job.get("name") or "").strip(),
                        location=_location(job.get("location")),
                        url=PUBLIC_URL.format(token=token, job_id=job_id),
                        source="smartrecruiters",
                        category=entry.get("category", ""),
                        job_function=((job.get("function") or {}).get("label") or ""),
                        posted_at=job.get("releasedDate"),
                    )
                )

            offset += PAGE_SIZE
            if offset >= int(data.get("totalFound") or 0):
                break

        return postings


def _location(location) -> str:
    """City, region, country -- skipping the parts a given posting omits.

    Country is upper-cased because the API returns a two-letter code in
    lower case ("us"), which reads as a typo next to "Austin, TX".
    """
    if not isinstance(location, dict):
        return ""
    city = (location.get("city") or "").strip()
    region = (location.get("region") or "").strip()
    country = (location.get("country") or "").strip().upper()
    parts = [p for p in (city, region, country) if p]
    place = ", ".join(parts)
    if location.get("remote"):
        return f"Remote - {place}" if place else "Remote"
    return place
