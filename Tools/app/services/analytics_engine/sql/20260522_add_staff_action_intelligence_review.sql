CREATE TABLE IF NOT EXISTS "AnalyticsEngine".staff_action_intelligence_review (
    id BIGSERIAL PRIMARY KEY,
    staff_key TEXT NOT NULL,
    username TEXT,
    email TEXT,
    phone TEXT,
    team TEXT,
    role_scope TEXT,
    window_days INT NOT NULL DEFAULT 7,
    window_start TIMESTAMP,
    window_end TIMESTAMP,
    overall_score NUMERIC,
    priority_score NUMERIC,
    risk TEXT,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    business_impact_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    key_findings_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    recommended_actions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    coaching_points_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    data_gaps_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_counts_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    llm_output_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    model TEXT,
    context_version TEXT,
    context_hash TEXT,
    error TEXT,
    rated_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT ux_staff_action_intelligence_staff_window UNIQUE (staff_key, role_scope, window_days)
);

CREATE INDEX IF NOT EXISTS idx_staff_action_intelligence_staff_window
    ON "AnalyticsEngine".staff_action_intelligence_review (staff_key, window_days);

CREATE INDEX IF NOT EXISTS idx_staff_action_intelligence_team
    ON "AnalyticsEngine".staff_action_intelligence_review (team);

CREATE INDEX IF NOT EXISTS idx_staff_action_intelligence_risk
    ON "AnalyticsEngine".staff_action_intelligence_review (risk);

CREATE INDEX IF NOT EXISTS idx_staff_action_intelligence_priority
    ON "AnalyticsEngine".staff_action_intelligence_review (priority_score DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_staff_action_intelligence_status
    ON "AnalyticsEngine".staff_action_intelligence_review (status);

CREATE INDEX IF NOT EXISTS idx_staff_action_intelligence_rated_at
    ON "AnalyticsEngine".staff_action_intelligence_review (rated_at DESC NULLS LAST);
