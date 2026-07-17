-- Create staging bridge for additional booking contacts from MySQL user_contact_info
CREATE TABLE IF NOT EXISTS "AnalyticsEngine".staging_user_contact_info (
    source_id bigint PRIMARY KEY,
    user_id bigint NULL,
    booking_id bigint NOT NULL,
    email text NULL,
    contact_name text NULL,
    mobile text NULL,
    normalized_mobile text NULL,
    added_by text NULL,
    added_on date NULL,
    synced_at timestamp without time zone NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_staging_user_contact_info_booking_id
    ON "AnalyticsEngine".staging_user_contact_info (booking_id);

CREATE INDEX IF NOT EXISTS idx_staging_user_contact_info_user_id
    ON "AnalyticsEngine".staging_user_contact_info (user_id);

CREATE INDEX IF NOT EXISTS idx_staging_user_contact_info_email
    ON "AnalyticsEngine".staging_user_contact_info (LOWER(email));

CREATE INDEX IF NOT EXISTS idx_staging_user_contact_info_normalized_mobile
    ON "AnalyticsEngine".staging_user_contact_info (normalized_mobile);
