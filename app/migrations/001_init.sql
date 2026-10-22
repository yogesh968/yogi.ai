-- 001_init: core schema for the personal call assistant.

CREATE TABLE IF NOT EXISTS app_settings (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS calls (
    id               bigserial PRIMARY KEY,
    direction        text NOT NULL CHECK (direction IN ('incoming', 'outgoing')),
    mode             text NOT NULL DEFAULT 'phone' CHECK (mode IN ('phone', 'test')),
    contact_name     text NOT NULL DEFAULT '',
    contact_phone    text NOT NULL DEFAULT '',
    purpose          text NOT NULL DEFAULT '',
    -- live -> queued -> processing -> done | failed
    status           text NOT NULL DEFAULT 'live'
                     CHECK (status IN ('live', 'queued', 'processing', 'done', 'failed')),
    started_at       timestamptz NOT NULL DEFAULT now(),
    ended_at         timestamptz,
    duration_s       integer,
    end_reason       text NOT NULL DEFAULT '',
    recording_path   text,
    voice            text NOT NULL DEFAULT '',
    title            text NOT NULL DEFAULT '',
    summary          text NOT NULL DEFAULT '',
    sentiment        text NOT NULL DEFAULT '',
    analysis         jsonb,
    transcript_text  text NOT NULL DEFAULT '',
    error            text NOT NULL DEFAULT '',
    -- 'simple' config: no English stemming, so Hindi/Hinglish words index as-is.
    search           tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(contact_name, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(summary, '') || ' ' || coalesce(purpose, '')), 'B') ||
        setweight(to_tsvector('simple', coalesce(transcript_text, '')), 'C')
    ) STORED
);
CREATE INDEX IF NOT EXISTS calls_search_idx ON calls USING gin (search);
CREATE INDEX IF NOT EXISTS calls_started_idx ON calls (started_at DESC);
CREATE INDEX IF NOT EXISTS calls_contact_idx ON calls (lower(contact_name), started_at DESC);
CREATE INDEX IF NOT EXISTS calls_status_idx ON calls (status);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id        bigserial PRIMARY KEY,
    call_id   bigint NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    source    text NOT NULL DEFAULT 'live' CHECK (source IN ('live', 'corrected')),
    seq       integer NOT NULL,
    speaker   text NOT NULL CHECK (speaker IN ('caller', 'assistant', 'owner')),
    text      text NOT NULL,
    start_ms  integer NOT NULL DEFAULT 0,
    end_ms    integer NOT NULL DEFAULT 0,
    interrupted boolean NOT NULL DEFAULT false,
    UNIQUE (call_id, source, seq)
);

CREATE TABLE IF NOT EXISTS memory_facts (
    id              bigserial PRIMARY KEY,
    fact            text NOT NULL CHECK (length(fact) BETWEEN 1 AND 1000),
    contact_name    text NOT NULL DEFAULT '',   -- '' = general fact about the owner
    source_call_id  bigint REFERENCES calls(id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS memory_facts_uniq ON memory_facts (lower(contact_name), lower(fact));

CREATE TABLE IF NOT EXISTS action_items (
    id          bigserial PRIMARY KEY,
    call_id     bigint NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    owner       text NOT NULL DEFAULT '',
    task        text NOT NULL,
    due         text NOT NULL DEFAULT '',
    done        boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS action_items_open_idx ON action_items (done) WHERE NOT done;

-- Postgres-backed job queue (claimed with FOR UPDATE SKIP LOCKED).
CREATE TABLE IF NOT EXISTS jobs (
    id            bigserial PRIMARY KEY,
    kind          text NOT NULL,
    payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
    status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'done', 'failed')),
    attempts      integer NOT NULL DEFAULT 0,
    max_attempts  integer NOT NULL DEFAULT 3,
    run_after     timestamptz NOT NULL DEFAULT now(),
    locked_at     timestamptz,
    last_error    text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_ready_idx ON jobs (run_after) WHERE status = 'pending';
