"""Cross-source duplicate detection.

The gap this closes: once discovery.py promotes a company to its own
direct ATS connector, that company's postings can arrive via TWO paths
at once -- an aggregator (Muse) that already indexed it, and the new
direct source. Nothing before this stopped both from showing as
separate 'open' rows for what's really the same posting. scheduler.py's
per-source upsert can't catch this on its own -- it only ever looks at
ONE source_entry's fresh postings at a time, and the collision is
BETWEEN two different sources' postings.

Design: a normalized `dedup_key` (company + title + location, cheaply
computed, stored on the row) plus a periodic, STATELESS sweep
(run_dedup_sweep) that ranks every group of open-or-duplicate postings
sharing a dedup_key by source precedence, keeps the top-ranked one
'open', and marks the rest 'duplicate' with `duplicate_of` pointing at
it. Stateless is the important property: the sweep recomputes the
winner from scratch every time it runs (one SQL statement, a window
function, no Python bookkeeping of "what did we already decide last
time") -- so if the canonical posting later closes, the next sweep
naturally promotes the next-best duplicate back to 'open' with no
special-case code for that transition. `status = 'closed'` postings are
never touched by this -- closing is scheduler.py's job (a source no
longer returning a posting), not a dedup outcome, and a closed posting
should stay closed even if it once had a dedup_key collision.

Precedence: a posting from a company's own direct ATS connector
(Greenhouse/Lever/Workday/Oracle Recruiting Cloud/jsonld) outranks the
same posting surfaced by an aggregator (Muse/Adzuna) -- the direct
source is more precise (a company-scoped fetch, not a keyword search
across everything) and more current (its own re-scrape cadence, not
whatever the aggregator's crawl happened to catch). USAJobs isn't
ranked against these at all in practice -- it's the federal government's
OWN board, a distinct data domain from private-company postings, so a
real collision with it would be a coincidence, not the pattern this was
built for. Ties (same precedence tier) are broken by first_seen: the
posting THIS project saw first stays canonical, so the "duplicate"
label doesn't flip-flop for no reason across sweeps.
"""
import re

DIRECT_SOURCE_ATS = {"greenhouse", "lever", "workday", "oracle_recruiting", "jsonld"}

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|corp|corporation|llc|ltd|limited|co|company|group|holdings|plc)\b\.?",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str, strip_legal_suffix: bool = False) -> str:
    text = (text or "").lower()
    if strip_legal_suffix:
        text = _LEGAL_SUFFIX_RE.sub("", text)
    text = _PUNCT_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def compute_dedup_key(company: str, title: str, location: str) -> str | None:
    """None (not an empty string) when there isn't enough to normalize --
    dedup.py's sweep explicitly ignores NULL keys, so a posting with a
    blank company or title never gets swept into a spurious group with
    every other under-described posting.

    Location is normalized but NOT truncated/simplified beyond
    whitespace+punctuation -- deliberately conservative. Collapsing
    "New York, NY" and "New York, New York, US" to the same short form
    would catch more true duplicates, but it would also risk merging
    genuinely different postings whose location strings happen to
    coincide after aggressive normalization. Under-merging (missing a
    real duplicate because the two sources format location differently)
    is the safer failure mode than over-merging (hiding a real distinct
    posting) -- it leaves today's status quo (no dedup at all) rather
    than making anything worse.
    """
    company_n = _normalize(company, strip_legal_suffix=True)
    title_n = _normalize(title)
    location_n = _normalize(location)
    if not company_n or not title_n:
        return None
    return f"{company_n}|{title_n}|{location_n}"


# Postgres CASE expression, not Python -- the sweep is one SQL statement
# so this precedence has to be inline SQL, generated once from the same
# DIRECT_SOURCE_ATS set defined above rather than hand-duplicated in the
# query string.
def _precedence_case_sql() -> str:
    direct = ", ".join(f"'{a}'" for a in sorted(DIRECT_SOURCE_ATS))
    return f"CASE WHEN ats IN ({direct}) THEN 0 ELSE 1 END"


def run_dedup_sweep(cur) -> int:
    """Re-ranks every dedup_key group among currently open-or-duplicate
    postings. Returns the number of rows whose status changed (either
    direction: open->duplicate or duplicate->open).

    Takes a live cursor rather than opening its own connection/transaction
    -- callers (scheduler.py's run_forever loop, or a standalone script)
    decide the transaction boundary.
    """
    precedence = _precedence_case_sql()
    cur.execute(
        f"""
        WITH ranked AS (
            SELECT id, dedup_key,
                   ROW_NUMBER() OVER (
                       PARTITION BY dedup_key
                       ORDER BY {precedence}, first_seen ASC, id ASC
                   ) AS rnk
            FROM postings
            WHERE status IN ('open', 'duplicate') AND dedup_key IS NOT NULL
        ),
        canonical AS (
            SELECT dedup_key, id AS canonical_id FROM ranked WHERE rnk = 1
        ),
        desired AS (
            SELECT ranked.id,
                   CASE WHEN ranked.rnk = 1 THEN 'open' ELSE 'duplicate' END AS desired_status,
                   CASE WHEN ranked.rnk = 1 THEN NULL ELSE canonical.canonical_id END AS desired_duplicate_of
            FROM ranked
            JOIN canonical ON canonical.dedup_key = ranked.dedup_key
        )
        UPDATE postings
        SET status = desired.desired_status,
            duplicate_of = desired.desired_duplicate_of
        FROM desired
        WHERE postings.id = desired.id
          AND (postings.status IS DISTINCT FROM desired.desired_status
               OR postings.duplicate_of IS DISTINCT FROM desired.desired_duplicate_of)
        """
    )
    return cur.rowcount
