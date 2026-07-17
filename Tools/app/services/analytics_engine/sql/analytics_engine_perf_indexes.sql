-- Run on source/staging tables to improve incremental fetches.
-- Adjust schema/table names if your staging schema differs.

CREATE INDEX IF NOT EXISTS idx_staging_call_log_tracking_source_id
    ON "AnalyticsEngine".staging_call_log_tracking (source_id);

CREATE INDEX IF NOT EXISTS idx_staging_call_log_tracking_source_id_call_time
    ON "AnalyticsEngine".staging_call_log_tracking (source_id)
    WHERE call_time IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_staging_user_account_source_id
    ON "AnalyticsEngine".staging_user_account (source_id);

CREATE INDEX IF NOT EXISTS idx_staging_booking_confirm_source_id
    ON "AnalyticsEngine".staging_booking_confirm (source_id);

CREATE INDEX IF NOT EXISTS idx_staging_lead_tracking_source_id
    ON "AnalyticsEngine".staging_lead_tracking (source_id);
