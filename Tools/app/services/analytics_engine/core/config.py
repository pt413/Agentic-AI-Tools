import os

SCHEMA_NAME = "AnalyticsEngine"

# staging tables
STAGING_CALL_LOG = f'"{SCHEMA_NAME}".staging_call_log_unified'
STAGING_USERS = f'"{SCHEMA_NAME}".staging_user_account'
STAGING_LEADS = f'"{SCHEMA_NAME}".staging_lead_tracking'
STAGING_BOOKINGS = f'"{SCHEMA_NAME}".staging_booking_confirm'
STAGING_USER_CONTACT_INFO = f'"{SCHEMA_NAME}".staging_user_contact_info'
STAGING_SITE_VISITS = f'"{SCHEMA_NAME}".staging_site_visits'
STAGING_TRAVEL_CART = f'"{SCHEMA_NAME}".staging_travel_cart'
STAGING_WISHLIST = f'"{SCHEMA_NAME}".staging_user_wishlist'
STAGING_WHATSAPP = f'"{SCHEMA_NAME}".staging_whatsapp_messages'
STAGING_WEB_VISITS = f'"{SCHEMA_NAME}".staging_web_visits'
STAGING_CHECKIN = f'"{SCHEMA_NAME}".staging_checkin_form'
STAGING_CHECKOUT = f'"{SCHEMA_NAME}".staging_checkout_form'
STAGING_TICKETS = f'"{SCHEMA_NAME}".staging_user_ticket'
STAGING_EMAILS = f'"{SCHEMA_NAME}".staging_email_messages'
STAGING_BOOKING_AUDIT = f'"{SCHEMA_NAME}".staging_booking_audit_history'
STAGING_BOOKING_INVOICE = f'"{SCHEMA_NAME}".staging_booking_invoice_details'

# identity tables
IDENTITY_PERSON = f'"{SCHEMA_NAME}".identity_person'
IDENTITY_PERSON_KEY = f'"{SCHEMA_NAME}".identity_person_key'
IDENTITY_PERSON_MERGE = f'"{SCHEMA_NAME}".identity_person_merge'

# event tables
EVENT_FACT = f'"{SCHEMA_NAME}".event_fact'
EVENT_PARTICIPANT = f'"{SCHEMA_NAME}".event_participant'
EVENT_CONTEXT = f'"{SCHEMA_NAME}".event_context'
EVENT_ONTOLOGY_TAG = f'"{SCHEMA_NAME}".event_ontology_tag'
CUSTOMER_CURRENT_STATE = f'"{SCHEMA_NAME}".customer_current_state'

# domain/business fact tables
BOOKING_FACT = f'"{SCHEMA_NAME}".booking_fact'
LEAD_FACT = f'"{SCHEMA_NAME}".lead_fact'

# operational tables
PROCESSOR_CHECKPOINT = f'"{SCHEMA_NAME}".processor_checkpoint'
STAGING_SYNC_CHECKPOINT = f'"{SCHEMA_NAME}".staging_sync_checkpoint'

# external source DB env strings
THIRD_PARTY_MYSQL_URL = os.getenv("MYSQL_DATABASE_URL")
THIRDPARTY_POSTGRES_URL = os.getenv("THIRDPARTY_POSTGRES_URL")

# source-engine mapping
STAGING_SOURCE_KIND_BY_TABLE = {
    "user_account": "mysql",
    "lead_tracking": "mysql",
    #"call_log_tracking": "mysql",
    "site_visits": "mysql",
    "travel_cart": "mysql",
    "user_wishlist": "mysql",
    "booking_confirm": "mysql",
    "user_contact_info": "mysql",
    "whatsapp_messages": "thirdparty_pg",
    "call_recordings_transcript": "thirdparty_pg",
    "web_visits": "thirdparty_pg",
    "checkin_form": "mysql",
    "checkout_form": "mysql",
    "user_ticket": "mysql",
    "email_messages": "thirdparty_pg",
    "booking_audit_history": "mysql",
    "booking_invoice_details": "mysql",
}

# -----------------------------------------------------------------------------
# Auto-added missing staging constants after analytics_engine folder reorg
# -----------------------------------------------------------------------------
STAGING_BUILDINGS = f'"{SCHEMA_NAME}".staging_buildings'
