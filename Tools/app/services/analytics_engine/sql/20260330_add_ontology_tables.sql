BEGIN;

CREATE TABLE IF NOT EXISTS "AnalyticsEngine".event_ontology_tag (
    event_ontology_tag_id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES "AnalyticsEngine".event_fact(event_id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    value TEXT NOT NULL,
    is_primary BOOLEAN NOT NULL DEFAULT TRUE,
    confidence NUMERIC(5,4),
    source TEXT,
    evidence_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (event_id, namespace, value)
);

CREATE INDEX IF NOT EXISTS idx_event_ontology_tag_event_id
    ON "AnalyticsEngine".event_ontology_tag (event_id);

CREATE INDEX IF NOT EXISTS idx_event_ontology_tag_namespace
    ON "AnalyticsEngine".event_ontology_tag (namespace, value);

CREATE TABLE IF NOT EXISTS "AnalyticsEngine".customer_current_state (
    anchor_type TEXT NOT NULL,
    anchor_id TEXT NOT NULL,
    person_id BIGINT,
    lead_id TEXT,
    booking_id TEXT,
    user_id TEXT,
    journey_stage TEXT,
    resolution_stage TEXT,
    relationship_health TEXT,
    ownership_team TEXT,
    next_best_action TEXT,
    last_event_id BIGINT REFERENCES "AnalyticsEngine".event_fact(event_id) ON DELETE SET NULL,
    last_event_time TIMESTAMPTZ,
    state_meta JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (anchor_type, anchor_id)
);

CREATE INDEX IF NOT EXISTS idx_customer_current_state_person
    ON "AnalyticsEngine".customer_current_state (person_id);

CREATE INDEX IF NOT EXISTS idx_customer_current_state_lead
    ON "AnalyticsEngine".customer_current_state (lead_id);

CREATE INDEX IF NOT EXISTS idx_customer_current_state_booking
    ON "AnalyticsEngine".customer_current_state (booking_id);

CREATE INDEX IF NOT EXISTS idx_customer_current_state_user
    ON "AnalyticsEngine".customer_current_state (user_id);

CREATE INDEX IF NOT EXISTS idx_customer_current_state_stage
    ON "AnalyticsEngine".customer_current_state (journey_stage, resolution_stage, relationship_health);

COMMIT;
