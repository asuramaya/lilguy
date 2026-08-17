-- Postgres schema for the live-service version of this project.
--
-- Replaces three things the git-committed batch pipeline used to be:
--   sources.yaml            -> sources
--   data/all_postings.json  -> postings
--   scrape.py's stdout log  -> scrape_runs
-- plus one new thing the batch pipeline never had: discovery_candidates,
-- the automated-verification queue described in docs/service-architecture.md.
--
-- Design choices worth knowing before you extend this:
--   * `config` is JSONB holding exactly the entry dict a connector's
--     fetch(entry) already expects (see scraper/connectors/base.py) --
--     company/ats/category/max_pages plus whatever ats-specific fields
--     that connector needs (tenant/wd_host/site for workday, host/
--     site_number for oracle_recruiting, sitemap_url/url_pattern for
--     jsonld, token for greenhouse/lever). This means NONE of the four
--     existing connectors needed to change to be reused here -- they
--     already take a plain dict and return list[Posting].
--   * status on `sources` mirrors the promotion state machine:
--     probation (just auto-promoted, unconfirmed) -> active (confirmed
--     twice) -> disabled (self-healed out after repeated failures).
--     rejected candidates that never passed the gate live in
--     discovery_candidates, not here, and are never deleted -- the
--     evidence of why something was rejected is itself useful data.

-- Used for the company-name fuzzy-match check in the auto-verification
-- gate (service/verify.py) and for idx_postings_title_trgm below.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS sources (
    id                   BIGSERIAL PRIMARY KEY,
    company              TEXT NOT NULL,
    ats                  TEXT NOT NULL,
    category             TEXT NOT NULL DEFAULT '',
    config               JSONB NOT NULL,
    status               TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('probation', 'active', 'disabled')),
    added_by             TEXT NOT NULL DEFAULT 'manual'
                         CHECK (added_by IN ('manual', 'discovery')),
    scrape_interval_seconds INTEGER NOT NULL DEFAULT 21600,  -- 6h default
    next_scrape_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_scrape_status   TEXT,
    last_scrape_error    TEXT,
    last_scraped_at      TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- one live row per (company, ats) -- a company can only be onboarded
    -- once per platform; re-discovering the same hit is a no-op, not a
    -- duplicate row.
    UNIQUE (company, ats)
);

CREATE INDEX IF NOT EXISTS idx_sources_due
    ON sources (next_scrape_at)
    WHERE status IN ('probation', 'active');

CREATE TABLE IF NOT EXISTS postings (
    id                   TEXT PRIMARY KEY,  -- same stable id scheme as Posting.id
    source_id            BIGINT REFERENCES sources(id) ON DELETE SET NULL,
    source_entry         TEXT NOT NULL,     -- sources.company at fetch time (kept even if source_id later changes)
    company              TEXT NOT NULL,
    title                TEXT NOT NULL,
    location             TEXT NOT NULL DEFAULT '',
    url                  TEXT NOT NULL,
    ats                  TEXT NOT NULL,
    category             TEXT NOT NULL DEFAULT '',
    posted_at            TEXT,
    description_snippet  TEXT NOT NULL DEFAULT '',
    -- 'duplicate' exists because the SAME real posting can legitimately
    -- arrive via two different sources -- e.g. an aggregator (Muse)
    -- already surfaces a company, then discovery.py later promotes that
    -- same company's own direct ATS connector. Both would otherwise show
    -- as separate 'open' rows for the same real internship. See
    -- service/dedup.py -- it's a distinct status from 'closed' on
    -- purpose: a duplicate posting is still genuinely open somewhere,
    -- just not the canonical row to SHOW, and dedup.py's sweep is
    -- stateless/idempotent so a posting can move between 'open' and
    -- 'duplicate' as the situation changes without ever touching
    -- 'closed' (which only scheduler.py's close-on-absence logic sets).
    status               TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'duplicate', 'closed')),
    -- Precedence-normalized (company, title, location) -- see
    -- service/dedup.py's normalize() for exactly how. NULL/empty is
    -- valid (dedup.py's sweep simply ignores those rows) rather than
    -- required, so a posting with too little normalizable text never
    -- blocks an insert.
    dedup_key            TEXT,
    -- Set only when status = 'duplicate' -- points at the canonical
    -- (higher-precedence, or equal-precedence-but-first-seen) posting
    -- for the same dedup_key. Self-referential FK, not enforced NOT
    -- NULL: a posting starts with duplicate_of = NULL and only gets
    -- one assigned by a dedup sweep finding a real collision.
    duplicate_of         TEXT REFERENCES postings(id) ON DELETE SET NULL,
    first_seen           TIMESTAMPTZ NOT NULL,
    last_seen            TIMESTAMPTZ NOT NULL,
    closed_at            TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_postings_dedup_key ON postings (dedup_key) WHERE dedup_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_postings_status ON postings (status);
CREATE INDEX IF NOT EXISTS idx_postings_source_entry ON postings (source_entry);
CREATE INDEX IF NOT EXISTS idx_postings_title_trgm ON postings USING gin (title gin_trgm_ops);

CREATE TABLE IF NOT EXISTS discovery_candidates (
    id                   BIGSERIAL PRIMARY KEY,
    company              TEXT NOT NULL,
    ats                  TEXT,              -- NULL until a probe finds a hit
    config               JSONB,
    review_status        TEXT NOT NULL DEFAULT 'unchecked'
                         CHECK (review_status IN ('unchecked', 'no_match', 'rejected', 'promoted')),
    evidence             JSONB,             -- {"total": N, "intern_count": N, "sample_titles": [...]}
    checked_at           TIMESTAMPTZ,
    next_check_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company)
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id                   BIGSERIAL PRIMARY KEY,
    -- SET NULL, not CASCADE: a rejected probation source gets DELETEd
    -- from `sources` (see scheduler.py's run_one), but its scrape_runs
    -- history -- the evidence of what actually failed -- should survive
    -- that deletion, not vanish with it.
    source_id            BIGINT REFERENCES sources(id) ON DELETE SET NULL,
    started_at           TIMESTAMPTZ NOT NULL,
    finished_at          TIMESTAMPTZ,
    fetched_count        INTEGER,
    internship_count     INTEGER,
    ok                   BOOLEAN,
    error                TEXT
);

CREATE INDEX IF NOT EXISTS idx_scrape_runs_source ON scrape_runs (source_id, started_at DESC);

-- A small, deliberately simple activity log for the "did something
-- notable just happen" question -- a new source promoted, an existing
-- one going dark after repeated failures, a backup restore-test result.
-- Not a general audit trail (scrape_runs and discovery_candidates.
-- evidence already cover the detailed history for their own domains) --
-- this exists so the frontend can show "N new since you last looked"
-- without the operator having to dig through the Sources/Discovery
-- tabs by hand. See docs/service-architecture.md's "Staying informed"
-- section for why this exists instead of an email/webhook notifier.
CREATE TABLE IF NOT EXISTS events (
    id                   BIGSERIAL PRIMARY KEY,
    kind                 TEXT NOT NULL
                         CHECK (kind IN ('promoted', 'disabled', 'reinstated', 'backup_restore_test')),
    company              TEXT,              -- NULL for backup_restore_test, which isn't about one source
    detail               TEXT NOT NULL,     -- short human-readable summary, e.g. "promoted via greenhouse"
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_created_at ON events (created_at DESC);

-- ---------------------------------------------------------------------
-- Additive column migrations
--
-- Everything above is CREATE ... IF NOT EXISTS, which is idempotent for
-- a table that does not exist yet but does NOTHING to a table that
-- already does -- so a column added to a definition above never reaches
-- a live database. db.py's header explains why there's no Alembic here
-- (five tables, hand-written SQL, not worth the abstraction yet); this
-- section is the small price of that choice. ADD COLUMN IF NOT EXISTS
-- is itself idempotent, so init_schema() stays safe to run on every
-- boot, which is how the migrate container already behaves.
--
-- Keep these append-only and additive. A change that has to rewrite or
-- drop data is the signal that this project has outgrown the approach
-- and wants a real migration chain instead.
-- ---------------------------------------------------------------------

-- Parsed form of postings.posted_at. The raw TEXT column is kept as
-- provenance -- providers disagree wildly (ISO-8601, epoch millis,
-- English prose like "Posted 30+ Days Ago"), and keeping the original
-- means a parser bug can be re-run against the source value rather than
-- against a lossy conversion. See service/posted_at.py.
ALTER TABLE postings ADD COLUMN IF NOT EXISTS posted_at_ts TIMESTAMPTZ;

-- True when the provider gave a bound or a coarse value rather than a
-- real timestamp ("30+ days ago", a date with no time). Lets the UI say
-- "30+ days ago" instead of implying a precision nobody offered.
ALTER TABLE postings ADD COLUMN IF NOT EXISTS posted_at_approx BOOLEAN NOT NULL DEFAULT FALSE;

-- NULLS LAST because a posting with no parseable date should sort to
-- the bottom of "newest first", not the top.
CREATE INDEX IF NOT EXISTS idx_postings_posted_at_ts ON postings (posted_at_ts DESC NULLS LAST);

-- Full posting text for the in-app posting page, structure preserved as
-- plain text (see scraper/connectors/util.py's to_display_text). Kept
-- separate from description_snippet rather than replacing it: the
-- snippet is whitespace-collapsed and capped at 600 chars because it
-- feeds keyword MATCHING (user_filter.passes), and widening that would
-- change what every preset matches. This column is for READING.
--
-- Empty rather than NULL means "the provider gave us nothing"; NULL
-- means "not fetched yet", which is the state Workday postings sit in
-- until service/workday_descriptions.py fills them (its list endpoint
-- carries no description at all).
ALTER TABLE postings ADD COLUMN IF NOT EXISTS description TEXT;

-- Normalized employer identity, so "every listing at this company" can be
-- one page even when the postings arrived through different sources with
-- different spellings ("Eaton" via jsonld, "Eaton Corporation" via muse).
-- Computed by dedup.compute_company_key -- the SAME normalization the
-- company component of dedup_key uses, shared deliberately so "same
-- company" can't mean two different things in two places.
ALTER TABLE postings ADD COLUMN IF NOT EXISTS company_key TEXT;
CREATE INDEX IF NOT EXISTS idx_postings_company_key ON postings (company_key);
