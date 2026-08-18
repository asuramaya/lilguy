"""Turns whatever an ATS calls a "posted date" into a real timestamp.

Each connector passes the provider's raw value straight through into
`postings.posted_at`, which is a TEXT column, and the providers do not
agree on a format. Live census of the open corpus:

    greenhouse / muse / jsonld / oracle
        ISO-8601, e.g. "2026-07-09T10:58:08-04:00"          ~84%
    lever
        epoch MILLISECONDS as a bare integer, e.g. 1625176335503
    workday
        English prose, e.g. "Posted Today", "Posted 2 Days Ago",
        "Posted 30+ Days Ago"

Because it was TEXT, nothing could sort or filter on it, so the feed
sorted and displayed `first_seen` -- when THIS system first saw the
posting -- under a column header reading "Posted". With a young
database that made every posting look hours old, including Lever rows
whose real posted date was 2021. For a tool whose whole job is finding
internships worth applying to, "how old is this really" is the most
decision-relevant fact on the row, and it was wrong.

Two values come back, because precision differs by provider and
flattening that away would replace one lie with a quieter one:

    ts      -- the best available timestamp, always timezone-aware UTC
    approx  -- True when the source only gave a bound or a coarse value,
               so the UI can say "30+ days ago" rather than implying a
               precision the provider never offered.

Workday's relative strings are resolved against `seen_at` (when the
scrape happened) rather than "now", so re-parsing an old row during a
backfill yields the same answer it would have at scrape time.
"""
import re
from datetime import datetime, timedelta, timezone

# "Posted 30+ Days Ago" is a CEILING marker, not a measurement -- Workday
# stops counting there, so the true age is "at least 30 days" and can be
# far more. Recorded as a lower bound flagged approximate rather than
# silently rendered as exactly 30 days old.
_WORKDAY_PLUS_RE = re.compile(r"posted\s+(\d+)\+\s*days?\s+ago", re.I)
_WORKDAY_N_DAYS_RE = re.compile(r"posted\s+(\d+)\s*days?\s+ago", re.I)
_WORKDAY_TODAY_RE = re.compile(r"posted\s+today", re.I)
_WORKDAY_YESTERDAY_RE = re.compile(r"posted\s+yesterday", re.I)

# Lever sends epoch milliseconds. Bare seconds would also be plausible
# from some provider, so discriminate by magnitude rather than assuming:
# 10^11 seconds is year 5138, and 10^11 milliseconds is 1973, so any
# value at or above this is milliseconds and anything below is seconds.
_MILLIS_THRESHOLD = 10**11


def parse_posted_at(raw, seen_at: datetime) -> tuple:
    """(ts, approx) -- ts is an aware UTC datetime, or None if the value
    is missing or in a format this doesn't recognize. `seen_at` anchors
    relative formats and must itself be timezone-aware.

    Never raises: a provider inventing a new format should make one row
    fall back to "unknown posted date", not break the scrape that found
    it.
    """
    if raw is None:
        return None, False

    if isinstance(raw, datetime):
        return _as_utc(raw), False

    text = str(raw).strip()
    if not text:
        return None, False

    # Numeric epoch (lever). Checked before ISO parsing because a bare
    # integer is not valid ISO and would otherwise fall through to None.
    if re.fullmatch(r"-?\d{9,}", text):
        try:
            value = int(text)
            seconds = value / 1000 if abs(value) >= _MILLIS_THRESHOLD else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc), False
        except (ValueError, OSError, OverflowError):
            return None, False

    # Workday relative prose. Ordered most-specific first: the "30+"
    # pattern has to win over the plain "N days" one, which would
    # otherwise match the same string and quietly drop the "+".
    lowered = text.lower()
    if "posted" in lowered:
        m = _WORKDAY_PLUS_RE.search(lowered)
        if m:
            return seen_at - timedelta(days=int(m.group(1))), True
        m = _WORKDAY_N_DAYS_RE.search(lowered)
        if m:
            # Day-granularity: flagged approximate because the provider
            # rounded to whole days, not because the value is a bound.
            return seen_at - timedelta(days=int(m.group(1))), True
        if _WORKDAY_TODAY_RE.search(lowered):
            return seen_at, True
        if _WORKDAY_YESTERDAY_RE.search(lowered):
            return seen_at - timedelta(days=1), True
        return None, False

    # ISO-8601. fromisoformat handles offsets natively on 3.11+ and a
    # trailing "Z" needs translating for older versions; date-only
    # values ("2026-07-09") parse as midnight, which is genuinely all
    # the precision the provider gave, so they're flagged approximate.
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None, False
    return _as_utc(parsed), len(text) <= 10


def _as_utc(dt: datetime) -> datetime:
    """A naive timestamp is treated as UTC rather than as local time --
    this runs in a container whose local zone is deliberately UTC, and
    guessing a zone would silently shift dates by up to a day."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
