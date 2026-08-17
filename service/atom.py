"""Renders a filtered feed as an Atom document.

This is the answer to "how do I find out about new postings without
watching the page", after a persistent notifier was ruled out: a Claude
Code cron job is session-bound and expires, and email/webhooks would
need SMTP or API credentials this project deliberately does not carry
(see docs/service-architecture.md's "Staying informed"). A feed needs
none of that -- no account, no key, no daemon on our side. The reader
polls, which also means the subscriber controls the frequency and can
unsubscribe by deleting a row in their own client.

Atom rather than RSS 2.0 because it makes the two things a job feed
needs unambiguous: `updated` is a required, properly-typed RFC-3339
timestamp (RSS's pubDate is optional and RFC-822), and `id` is a defined
permanent identifier readers use for dedup, so a posting re-appearing in
a later poll doesn't resurface as new.

Written by hand rather than with a library: the document is ~15 lines of
XML, and the project's stated ethos (CONTRIBUTING.md) is to avoid
dependencies a task doesn't need. The one real hazard is escaping, which
xml.sax.saxutils covers.
"""
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

# Atom requires a globally unique, permanent feed id. A tag: URI (RFC
# 4151) is the right shape for something without a guaranteed public
# URL, and unlike a http:// id it stays stable if the service moves
# host -- which matters because readers key their dedup off it.
FEED_ID_PREFIX = "tag:internship-feed,2026:"


def _rfc3339(value) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return _rfc3339(datetime.now(timezone.utc))
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry(posting: dict) -> str:
    title = posting.get("title") or "Untitled posting"
    company = posting.get("company") or ""
    location = posting.get("location") or ""
    category = posting.get("category") or ""
    url = posting.get("url") or ""

    # `updated` drives ordering and "is this new" in most readers. The
    # employer's own posting date is the honest answer where we have one;
    # first_seen is the fallback. Using first_seen unconditionally would
    # make every posting look brand new on the day this feed discovered
    # it, which is exactly the misreading the posted_at work removed from
    # the web UI -- no point reintroducing it here.
    updated = posting.get("posted_at_ts") or posting.get("first_seen")

    approx_note = ""
    if posting.get("posted_at_ts") and posting.get("posted_at_approx"):
        approx_note = " (date approximate — the source only gave a rounded or bounded age)"

    summary_parts = [p for p in (company, location, category) if p]
    summary = " · ".join(summary_parts) + approx_note

    # The posting id is already globally unique and stable across
    # re-scrapes (f"{source}:{company}:{external_id}"), which is exactly
    # what an Atom id needs, so it's reused rather than invented afresh.
    entry_id = FEED_ID_PREFIX + "posting/" + (posting.get("id") or url)

    return f"""  <entry>
    <title>{escape(title)}</title>
    <id>{escape(entry_id)}</id>
    <link rel="alternate" href={quoteattr(url)}/>
    <updated>{_rfc3339(updated)}</updated>
    <author><name>{escape(company or "Unknown company")}</name></author>
    <summary>{escape(summary)}</summary>
  </entry>"""


def render_atom(postings: list, *, title: str, self_url: str, feed_slug: str) -> str:
    """`self_url` is echoed back as rel="self" so a reader can rediscover
    the exact query it subscribed to (preset, search, freshness) from the
    document alone."""
    # A feed's own `updated` should be its newest entry, not "now" --
    # otherwise every poll looks like a change and well-behaved readers
    # lose the cheap "nothing happened" path.
    newest = max(
        (p.get("posted_at_ts") or p.get("first_seen") for p in postings if
         p.get("posted_at_ts") or p.get("first_seen")),
        default=None,
    )

    entries = "\n".join(_entry(p) for p in postings)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>{escape(title)}</title>
  <id>{escape(FEED_ID_PREFIX + feed_slug)}</id>
  <updated>{_rfc3339(newest)}</updated>
  <link rel="self" href={quoteattr(self_url)}/>
  <subtitle>Internship postings sourced directly from company career sites.</subtitle>
{entries}
</feed>
"""
