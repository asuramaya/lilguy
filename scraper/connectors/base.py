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
    # The EMPLOYER'S INDUSTRY, from sources.yaml, e.g. "Industrial
    # Manufacturing". A direct board knows this because the source IS one
    # company. An aggregator does not, and must leave it empty rather
    # than put something else here -- these two fields used to share one
    # column and the vocabularies were disjoint.
    category: str = ""
    # The JOB'S FUNCTION, e.g. "Software Engineering". The opposite case:
    # an aggregator that indexes by function knows this per posting,
    # while a direct board has no idea and leaves it empty. Neither field
    # is ever inferred from the other; a blank means "not known from this
    # source", which is information a guess would destroy.
    job_function: str = ""
    posted_at: Optional[str] = None  # ISO date string if the ATS provides one
    # Short, whitespace-collapsed text used for keyword MATCHING (see
    # user_filter.passes). Not for display.
    description_snippet: str = ""
    # The full posting text, structure preserved, for READING on a
    # posting page. Empty when the provider's list endpoint carries no
    # description -- Workday is the notable case, its list response has
    # none at all, so that arrives later via a per-posting fetch.
    description: str = ""
    extra: dict = field(default_factory=dict)
    # Which sources.yaml ENTRY this came from (its own `company:` label,
    # e.g. "UPS", "The Muse (aggregator...)") — set by scrape.py's
    # fetch_all() after fetch() returns, not by the connector itself.
    # NOT the same thing as `source` (the ats TYPE, e.g. "greenhouse") —
    # several entries can share an ats type but each is its own source
    # for the purpose of "did fetching THIS one succeed this run." See
    # store.py's rebuild() for why this matters: a source that fails
    # outright must not have its previously-known postings silently
    # treated as closed, and distinguishing "this entry's fetch failed"
    # from "this entry's fetch succeeded and returned nothing" requires
    # knowing which entry a stored posting actually came from.
    source_entry: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "company": self.company,
            "title": self.title,
            "location": self.location,
            "url": self.url,
            "source": self.source,
            "category": self.category,
            "job_function": self.job_function,
            "posted_at": self.posted_at,
            "description_snippet": self.description_snippet,
            "description": self.description,
            "source_entry": self.source_entry,
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
