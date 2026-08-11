from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Posting:
    """A single job/internship posting, normalized across ATS providers."""

    id: str  # stable, unique within this source: f"{source}:{company}:{external_id}"
    company: str
    title: str
    location: str
    url: str
    source: str  # ats connector name, e.g. "greenhouse"
    category: str = ""  # from sources.yaml, e.g. "Industrial Manufacturing"
    posted_at: Optional[str] = None  # ISO date string if the ATS provides one
    description_snippet: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "company": self.company,
            "title": self.title,
            "location": self.location,
            "url": self.url,
            "source": self.source,
            "category": self.category,
            "posted_at": self.posted_at,
            "description_snippet": self.description_snippet,
        }


class Connector:
    """Base class for an ATS connector. Subclasses implement fetch()."""

    name = "base"

    def fetch(self, entry: dict) -> list[Posting]:
        """entry is one item from sources.yaml. Return normalized Postings.

        Must not raise on an empty/zero-result board (that's a legitimate
        state — see Koch's Greenhouse board, which is valid but often
        empty). Must raise a clear error on a genuinely broken config
        (wrong token, unexpected response shape) rather than silently
        returning nothing — a silent empty result is indistinguishable
        from "no postings right now" and hides misconfiguration.
        """
        raise NotImplementedError
