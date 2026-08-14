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
