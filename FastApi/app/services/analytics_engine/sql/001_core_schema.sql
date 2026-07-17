CREATE SCHEMA IF NOT EXISTS "AnalyticsEngine";

CREATE TABLE IF NOT EXISTS "AnalyticsEngine".identity_person(
    person_id BIGSERIAL PRIMARY KEY,
    canonical_name TEXT,
    primary_phone VARCHAR(32),
    primary_email VARCHAR(255),
    person_kind VARCHAR(20) NOT NULL DEFAULT 'unknown',
    kind_confidence NUMERIC NOT NULL DEFAULT 0,
    merged_into_person_id BIGINT REFERENCES "AnalyticsEngine".identity_person(person_id),
    first_seen_at TIMESTAMP,
    last_seen_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS "AnalyticsEngine".identity_person_key(
    person_key_id BIGSERIAL PRIMARY KEY,
    person_id BIGINT NOT NULL REFERENCES "AnalyticsEngine".identity_person(person_id),
    key_type VARCHAR(50) NOT NULL,
    key_value TEXT NOT NULL,
    source_table VARCHAR(100),
    source_id VARCHAR(100),
    first_seen_at TIMESTAMP,
    last_seen_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_person_key_type_value
    ON "AnalyticsEngine".identity_person_key(key_type, key_value);

CREATE INDEX IF NOT EXISTS idx_identity_person_key_person_id
    ON "AnalyticsEngine".identity_person_key(person_id);

CREATE TABLE IF NOT EXISTS "AnalyticsEngine".identity_person_merge(
    person_merge_id BIGSERIAL PRIMARY KEY,
    canonical_person_id BIGINT NOT NULL REFERENCES "AnalyticsEngine".identity_person(person_id),
    merged_person_id BIGINT NOT NULL REFERENCES "AnalyticsEngine".identity_person(person_id),
    merge_reason VARCHAR(100) NOT NULL DEFAULT 'identity_resolution',
    merge_source_table VARCHAR(100),
    merge_source_id VARCHAR(100),
    merged_at TIMESTAMP NOT NULL DEFAULT NOW(),
    notes TEXT,
    CONSTRAINT chk_identity_person_merge_distinct
        CHECK (canonical_person_id <> merged_person_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_person_merge_pair
    ON "AnalyticsEngine".identity_person_merge(canonical_person_id, merged_person_id);

CREATE TABLE IF NOT EXISTS "AnalyticsEngine".event_fact(
    event_id BIGSERIAL PRIMARY KEY,
    event_family VARCHAR(40) NOT NULL,
    event_name VARCHAR(80) NOT NULL,
    event_direction VARCHAR(20),
    event_channel VARCHAR(30),
    event_time TIMESTAMP NOT NULL,
    event_end_time TIMESTAMP,
    event_status VARCHAR(40),
    metric_value NUMERIC,
    metric_unit VARCHAR(20),
    metric_name VARCHAR(40),
    event_meta JSONB DEFAULT '{}'::jsonb,
    source_table VARCHAR(100) NOT NULL,
    source_id VARCHAR(100) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_event_fact_source_identity
    ON "AnalyticsEngine".event_fact(source_table, source_id, event_name, event_time);

CREATE INDEX IF NOT EXISTS idx_event_fact_source
    ON "AnalyticsEngine".event_fact(source_table, source_id);

CREATE TABLE IF NOT EXISTS "AnalyticsEngine".event_participant(
    event_participant_id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES "AnalyticsEngine".event_fact(event_id) ON DELETE CASCADE,
    participant_seq SMALLINT NOT NULL,
    person_id BIGINT NOT NULL REFERENCES "AnalyticsEngine".identity_person(person_id),
    participant_role VARCHAR(40),
    direction_role VARCHAR(20),
    raw_key_type VARCHAR(50) NOT NULL,
    raw_key_value TEXT NOT NULL,
    raw_label TEXT,
    resolution_method VARCHAR(30),
    resolved_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_event_participant_event_seq
    ON "AnalyticsEngine".event_participant(event_id, participant_seq);

CREATE INDEX IF NOT EXISTS idx_event_participant_person_id
    ON "AnalyticsEngine".event_participant(person_id);

CREATE INDEX IF NOT EXISTS idx_event_participant_raw_key
    ON "AnalyticsEngine".event_participant(raw_key_type, raw_key_value);

CREATE TABLE IF NOT EXISTS "AnalyticsEngine".event_context (
    event_context_id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES "AnalyticsEngine".event_fact(event_id) ON DELETE CASCADE,
    context_type VARCHAR(40) NOT NULL,
    context_value TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_event_context_event
    ON "AnalyticsEngine".event_context(event_id);

CREATE INDEX IF NOT EXISTS idx_event_context_type_value
    ON "AnalyticsEngine".event_context(context_type, context_value);

CREATE UNIQUE INDEX IF NOT EXISTS uq_event_context_unique
    ON "AnalyticsEngine".event_context(event_id, context_type, context_value);

CREATE TABLE IF NOT EXISTS "AnalyticsEngine".booking_fact(
    booking_fact_id BIGSERIAL PRIMARY KEY,
    event_id BIGINT REFERENCES "AnalyticsEngine".event_fact(event_id) ON DELETE SET NULL,
    source_table VARCHAR(100) NOT NULL,
    source_id VARCHAR(100) NOT NULL,
    booking_id TEXT,
    lead_id TEXT,
    property_id TEXT,
    customer_phone VARCHAR(32),
    sales_phone VARCHAR(32),
    executive_ref VARCHAR(100),
    booking_status VARCHAR(40),
    booking_amount NUMERIC,
    currency_code VARCHAR(16),
    booking_time TIMESTAMP,
    raw_payload JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_booking_fact_source_identity
    ON "AnalyticsEngine".booking_fact(source_table, source_id);

CREATE INDEX IF NOT EXISTS idx_booking_fact_event
    ON "AnalyticsEngine".booking_fact(event_id);

CREATE INDEX IF NOT EXISTS idx_booking_fact_booking_id
    ON "AnalyticsEngine".booking_fact(booking_id);

CREATE INDEX IF NOT EXISTS idx_booking_fact_lead_id
    ON "AnalyticsEngine".booking_fact(lead_id);

CREATE INDEX IF NOT EXISTS idx_booking_fact_property_id
    ON "AnalyticsEngine".booking_fact(property_id);

CREATE TABLE IF NOT EXISTS "AnalyticsEngine".processor_checkpoint (
    processor_name VARCHAR(100) PRIMARY KEY,
    source_table VARCHAR(200) NOT NULL,
    cursor_mode VARCHAR(20) NOT NULL DEFAULT 'id',
    last_id BIGINT,
    last_timestamp TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_batch_count INTEGER NOT NULL DEFAULT 0,
    last_status VARCHAR(20) NOT NULL DEFAULT 'IDLE',
    last_error TEXT,
    notes TEXT
);


BEGIN;

CREATE TABLE IF NOT EXISTS "AnalyticsEngine".lead_fact(
    lead_fact_id BIGSERIAL PRIMARY KEY,
    event_id BIGINT REFERENCES "AnalyticsEngine".event_fact(event_id) ON DELETE SET NULL,
    source_table VARCHAR(100) NOT NULL,
    source_id VARCHAR(100) NOT NULL,
    lead_id TEXT,
    booking_id TEXT,
    user_id TEXT,
    person_id TEXT,
    actor_id TEXT,
    executive_ref VARCHAR(100),
    assigned_to VARCHAR(100),
    added_by VARCHAR(100),
    generated_by VARCHAR(100),
    origin TEXT,
    raw_status VARCHAR(80),
    is_resolved BOOLEAN,
    match_type VARCHAR(50),
    resolved_at TIMESTAMP,
    created_at_source TIMESTAMP,
    closed_at_source TIMESTAMP,
    synced_at_source TIMESTAMP,
    priority NUMERIC,
    contact_number VARCHAR(32),
    contact_number_alt VARCHAR(32),
    email VARCHAR(255),
    raw_payload JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_fact_source_identity
    ON "AnalyticsEngine".lead_fact(source_table, source_id);

CREATE INDEX IF NOT EXISTS idx_lead_fact_event
    ON "AnalyticsEngine".lead_fact(event_id);

CREATE INDEX IF NOT EXISTS idx_lead_fact_lead_id
    ON "AnalyticsEngine".lead_fact(lead_id);

CREATE INDEX IF NOT EXISTS idx_lead_fact_booking_id
    ON "AnalyticsEngine".lead_fact(booking_id);

CREATE INDEX IF NOT EXISTS idx_lead_fact_user_id
    ON "AnalyticsEngine".lead_fact(user_id);

CREATE INDEX IF NOT EXISTS idx_lead_fact_contact_number
    ON "AnalyticsEngine".lead_fact(contact_number);

CREATE INDEX IF NOT EXISTS idx_lead_fact_email
    ON "AnalyticsEngine".lead_fact(email);

CREATE TABLE IF NOT EXISTS "AnalyticsEngine".staging_sync_checkpoint (
    sync_name         TEXT PRIMARY KEY,
    last_id           BIGINT NULL,
    last_timestamp    TIMESTAMP NULL,
    updated_at        TIMESTAMP NOT NULL DEFAULT NOW(),
    last_batch_count  INTEGER NOT NULL DEFAULT 0,
    last_status       TEXT NULL,
    notes             TEXT NULL
);

TRUNCATE TABLE
    "AnalyticsEngine".lead_fact,
    "AnalyticsEngine".booking_fact,
    "AnalyticsEngine".event_context,
    "AnalyticsEngine".event_participant,
    "AnalyticsEngine".event_fact,
    "AnalyticsEngine".identity_person_merge,
    "AnalyticsEngine".identity_person_key,
    "AnalyticsEngine".identity_person,
    "AnalyticsEngine".processor_checkpoint
RESTART IDENTITY CASCADE;
TRUNCATE TABLE
    "AnalyticsEngine".event_context,
    "AnalyticsEngine".event_participant,
    "AnalyticsEngine".event_fact,
    "AnalyticsEngine".lead_fact,
    "AnalyticsEngine".booking_fact,
    "AnalyticsEngine".identity_person_key,
    "AnalyticsEngine".identity_person_merge,
    "AnalyticsEngine".identity_person,
    "AnalyticsEngine".processor_checkpoint,
    "AnalyticsEngine".staging_sync_checkpoint,
    "AnalyticsEngine".customer_current_state,
    "AnalyticsEngine".event_ontology_tag
CASCADE;

COMMIT;