-- 002: cloud telephony (Vobiz): contacts by phone number, provider call ids, phone-keyed memory.

ALTER TABLE calls ADD COLUMN IF NOT EXISTS provider_call_id text NOT NULL DEFAULT '';
ALTER TABLE calls ADD COLUMN IF NOT EXISTS hangup_cause text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS calls_phone_idx ON calls (contact_phone, started_at DESC) WHERE contact_phone <> '';
CREATE INDEX IF NOT EXISTS calls_provider_idx ON calls (provider_call_id) WHERE provider_call_id <> '';

-- Address book: E.164 phone -> name. Filled from the dashboard or automatically when a caller says their name.
CREATE TABLE IF NOT EXISTS contacts (
    phone       text PRIMARY KEY CHECK (phone ~ '^\+[0-9]{6,15}$'),
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    source      text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'auto')),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS contact_phone text NOT NULL DEFAULT '';
DROP INDEX IF EXISTS memory_facts_uniq;
CREATE UNIQUE INDEX IF NOT EXISTS memory_facts_uniq2 ON memory_facts (lower(contact_name), contact_phone, lower(fact));
