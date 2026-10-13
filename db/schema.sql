-- Outsmart AI metrics — schema
-- Raw facts live in these tables; metrics are computed in the views (db/views.sql).
-- Idempotent: safe to apply many times.

CREATE TABLE IF NOT EXISTS teams (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS people (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    team_id     INTEGER REFERENCES teams(id),
    joined_on   DATE,
    left_on     DATE,
    is_bot      BOOLEAN NOT NULL DEFAULT FALSE
);

-- A person has several accounts: git email, GitHub login, Cursor email, Claude email...
CREATE TABLE IF NOT EXISTS identities (
    id          SERIAL PRIMARY KEY,
    person_id   INTEGER REFERENCES people(id) ON DELETE CASCADE,  -- NULL = not linked yet
    source      TEXT NOT NULL CHECK (source IN ('git_email','github','cursor','claude','claude_api_key')),
    value       TEXT NOT NULL,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, value)
);

CREATE TABLE IF NOT EXISTS ai_licenses (
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    tool        TEXT NOT NULL,                 -- claude_code | cursor
    valid_from  DATE NOT NULL,
    valid_to    DATE,                          -- NULL = still active
    PRIMARY KEY (person_id, tool, valid_from)
);

-- One row per person, tool, day and source. The view keeps the source with the most usage (as Swarmia does).
CREATE TABLE IF NOT EXISTS ai_daily_activity (
    identity_id      INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
    tool             TEXT NOT NULL,
    day              DATE NOT NULL,
    source           TEXT NOT NULL,            -- analytics_api | otel | cursor_api
    is_active        BOOLEAN NOT NULL DEFAULT FALSE,
    sessions         INTEGER NOT NULL DEFAULT 0,
    lines_added      INTEGER NOT NULL DEFAULT 0,
    lines_removed    INTEGER NOT NULL DEFAULT 0,
    actions_accepted INTEGER NOT NULL DEFAULT 0,
    actions_rejected INTEGER NOT NULL DEFAULT 0,
    chat_requests    INTEGER NOT NULL DEFAULT 0,
    agent_requests   INTEGER NOT NULL DEFAULT 0,
    commits          INTEGER NOT NULL DEFAULT 0,
    pull_requests    INTEGER NOT NULL DEFAULT 0,
    tokens           BIGINT  NOT NULL DEFAULT 0,
    cost_usd         NUMERIC(12,4) NOT NULL DEFAULT 0,
    last_activity_at TIMESTAMPTZ,              -- only OTel has a time; daily APIs leave it NULL
    PRIMARY KEY (identity_id, tool, day, source)
);

CREATE TABLE IF NOT EXISTS repositories (
    id          SERIAL PRIMARY KEY,
    full_name   TEXT NOT NULL UNIQUE,          -- owner/repo
    team_id     INTEGER REFERENCES teams(id)
);

CREATE TABLE IF NOT EXISTS pull_requests (
    id               BIGINT PRIMARY KEY,       -- GitHub global id
    repo_id          INTEGER NOT NULL REFERENCES repositories(id),
    number           INTEGER NOT NULL,
    title            TEXT,
    author_login     TEXT,
    author_identity  INTEGER REFERENCES identities(id),
    initiated_by     INTEGER REFERENCES people(id),  -- the real person (for agent PRs)
    base_branch      TEXT,
    labels           TEXT[] NOT NULL DEFAULT '{}',
    is_draft         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL,
    first_commit_at  TIMESTAMPTZ,
    first_review_at  TIMESTAMPTZ,
    merged_at        TIMESTAMPTZ,
    closed_at        TIMESTAMPTZ,
    merge_commit_sha TEXT,
    additions        INTEGER NOT NULL DEFAULT 0,
    deletions        INTEGER NOT NULL DEFAULT 0,
    changed_files    INTEGER NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL,
    UNIQUE (repo_id, number)
);

CREATE TABLE IF NOT EXISTS commits (
    sha              TEXT NOT NULL,
    repo_id          INTEGER NOT NULL REFERENCES repositories(id),
    pr_id            BIGINT REFERENCES pull_requests(id) ON DELETE CASCADE,
    position         INTEGER,                  -- order within the PR (0 = first)
    author_name      TEXT,
    author_email     TEXT,
    author_login     TEXT,
    committer_name   TEXT,
    committer_email  TEXT,
    committer_login  TEXT,
    co_authors       TEXT[] NOT NULL DEFAULT '{}',   -- "Name <email>"
    trailers         TEXT[] NOT NULL DEFAULT '{}',   -- "Key: value"
    message_head     TEXT,
    committed_at     TIMESTAMPTZ,
    additions        INTEGER,
    deletions        INTEGER,
    PRIMARY KEY (repo_id, sha, pr_id)
);
CREATE INDEX IF NOT EXISTS commits_sha_idx ON commits (sha);

CREATE TABLE IF NOT EXISTS pr_reviews (
    id               BIGINT PRIMARY KEY,
    pr_id            BIGINT NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
    reviewer_login   TEXT,
    state            TEXT,
    submitted_at     TIMESTAMPTZ,
    comments         INTEGER NOT NULL DEFAULT 0
);

-- Output of the detection rules. Fully recalculated per PR on every run.
CREATE TABLE IF NOT EXISTS pr_ai_attribution (
    pr_id       BIGINT NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
    tool        TEXT NOT NULL,                 -- claude_code | cursor | copilot | git_ai
    mode        TEXT NOT NULL CHECK (mode IN ('assisted','cloud_agent','review_agent')),
    confidence  TEXT NOT NULL CHECK (confidence IN ('high','low')),
    reason      TEXT NOT NULL,
    PRIMARY KEY (pr_id, tool, mode)
);

-- Git AI: per-commit stats, from `git ai stats <sha> --json`
CREATE TABLE IF NOT EXISTS git_ai_commit_stats (
    repo_id                INTEGER NOT NULL REFERENCES repositories(id),
    sha                    TEXT NOT NULL,
    has_note               BOOLEAN NOT NULL,
    human_additions        INTEGER NOT NULL DEFAULT 0,
    unknown_additions      INTEGER NOT NULL DEFAULT 0,
    ai_additions           INTEGER NOT NULL DEFAULT 0,
    ai_accepted            INTEGER NOT NULL DEFAULT 0,
    git_diff_added_lines   INTEGER NOT NULL DEFAULT 0,
    git_diff_deleted_lines INTEGER NOT NULL DEFAULT 0,
    tool_model_breakdown   JSONB NOT NULL DEFAULT '{}'::jsonb,
    collected_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_id, sha)
);

-- Survival of each PR's lines on the main branch (git blame), measured periodically.
CREATE TABLE IF NOT EXISTS line_survival (
    pr_id            BIGINT NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
    measured_at      DATE NOT NULL,
    surviving_lines  INTEGER NOT NULL,
    PRIMARY KEY (pr_id, measured_at)
);

-- Phase 4: deployments and incidents for the change failure rate
CREATE TABLE IF NOT EXISTS deployments (
    id           TEXT PRIMARY KEY,
    repo_id      INTEGER NOT NULL REFERENCES repositories(id),
    environment  TEXT NOT NULL DEFAULT 'production',
    sha          TEXT NOT NULL,
    deployed_at  TIMESTAMPTZ NOT NULL
);

-- PRs included in each deployment (filled in by the collector from merge_commit_sha)
CREATE TABLE IF NOT EXISTS deployment_prs (
    deployment_id TEXT NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
    pr_id         BIGINT NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
    PRIMARY KEY (deployment_id, pr_id)
);

CREATE TABLE IF NOT EXISTS incidents (
    id                     TEXT PRIMARY KEY,
    title                  TEXT,
    opened_at              TIMESTAMPTZ NOT NULL,
    resolved_at            TIMESTAMPTZ,
    caused_by_deployment   TEXT REFERENCES deployments(id)
);

-- Last value of each cumulative OTel series, to turn it into deltas
CREATE TABLE IF NOT EXISTS otel_series (
    key         TEXT PRIMARY KEY,
    start_ns    BIGINT NOT NULL,
    value       DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
