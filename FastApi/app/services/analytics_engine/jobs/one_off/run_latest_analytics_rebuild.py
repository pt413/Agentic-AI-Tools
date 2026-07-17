# -----------------------------------------------------------------------------
# Repo bootstrap
# -----------------------------------------------------------------------------
import os
import sys
from pathlib import Path

def _analytics_engine_project_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent, *current.parents]:
        if (parent / "app").is_dir():
            return parent
    raise RuntimeError("Could not find FastApi project root containing the app/ folder.")

PROJECT_ROOT = _analytics_engine_project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# run_latest_analytics_rebuild.py
import json

from sqlalchemy import text

from app.db.database import get_db

from app.services.analytics_engine.processors.user_account_sync import UserAccountSync
from app.services.analytics_engine.processors.lead_sync import LeadSync
from app.services.analytics_engine.processors.call_log_sync import CallLogSync
from app.services.analytics_engine.processors.booking_confirm_sync import BookingConfirmSync
from app.services.analytics_engine.processors.user_contact_info_sync import UserContactInfoSync

from app.services.analytics_engine.processors.checkin_form_sync import CheckinFormSync
from app.services.analytics_engine.processors.checkout_form_sync import CheckoutFormSync
from app.services.analytics_engine.processors.email_message_sync import EmailMessageSync
from app.services.analytics_engine.processors.whatsapp_message_sync import WhatsAppMessageSync
from app.services.analytics_engine.processors.booking_audit_history_sync import BookingAuditHistorySync
from app.services.analytics_engine.processors.booking_invoice_details_sync import BookingInvoiceDetailsSync
from app.services.analytics_engine.processors.user_ticket_sync import UserTicketSync
from app.services.analytics_engine.processors.user_wishlist_sync import UserWishlistSync


SCHEMA = "AnalyticsEngine"
WINDOW = 1000000
DEFAULT_SKIP_COUNTS = False


def _checkpoint_safe_last_id(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value

    value_text = str(value).strip()
    if value_text.isdigit() or (value_text.startswith("-") and value_text[1:].isdigit()):
        try:
            return int(value_text)
        except ValueError:
            return None
    return None


def _build_notes(message: str, *, source_id_text=None):
    payload = {"message": str(message).strip()}
    if source_id_text not in (None, "") and _checkpoint_safe_last_id(source_id_text) is None:
        payload["cursor_source_id_text"] = str(source_id_text).strip()
    return json.dumps(payload, ensure_ascii=False)


def get_latest_id_start(db, table_name: str, window: int = WINDOW) -> int:
    max_id = db.execute(
        text(f'SELECT COALESCE(MAX(source_id), 0) FROM "{SCHEMA}"."{table_name}"')
    ).scalar() or 0
    return max(0, int(max_id) - int(window))


def get_latest_time_cursor_start(
    db,
    table_name: str,
    ts_expr: str,
    id_expr: str = "source_id",
    window: int = WINDOW,
):
    """
    For time-cursor processors, compute the oldest (timestamp, source_id)
    inside the latest N rows, ordered by timestamp desc and a source_id sort key
    that behaves numerically for numeric ids and lexically for text ids.

    The processor fetch logic is generally:
      ts > last_timestamp
      OR (ts = last_timestamp AND source_id > last_source_id)

    So we seed the checkpoint with the boundary row from the latest window.
    """
    source_text_expr = f"CAST({id_expr} AS TEXT)"
    source_sort_expr = (
        f"CASE WHEN {source_text_expr} ~ '^[0-9]+$' "
        f"THEN LPAD({source_text_expr}, 30, '0') ELSE {source_text_expr} END"
    )
    sql = text(
        f"""
        WITH latest_window AS (
            SELECT
                {source_text_expr} AS source_id_text,
                {source_sort_expr} AS source_sort_key,
                {ts_expr} AS cursor_ts
            FROM "{SCHEMA}"."{table_name}"
            WHERE {ts_expr} IS NOT NULL
            ORDER BY {ts_expr} DESC, {source_sort_expr} DESC
            LIMIT :window
        )
        SELECT
            source_id_text AS start_source_id,
            cursor_ts AS start_timestamp
        FROM latest_window
        ORDER BY cursor_ts ASC, source_sort_key ASC
        LIMIT 1
        """
    )
    row = db.execute(sql, {"window": int(window)}).mappings().fetchone()
    if not row:
        return None, None
    return row["start_source_id"], row["start_timestamp"]


def seed_time_cursor_checkpoint(
    db,
    processor_name: str,
    source_table: str,
    start_source_id,
    start_timestamp,
):
    db.execute(
        text(
            f"""
            INSERT INTO "{SCHEMA}".processor_checkpoint
            (
                processor_name,
                source_table,
                cursor_mode,
                last_id,
                last_timestamp,
                last_batch_count,
                last_status,
                notes,
                updated_at
            )
            VALUES
            (
                :processor_name,
                :source_table,
                'time',
                :last_id,
                :last_timestamp,
                0,
                'IDLE',
                :notes,
                NOW()
            )
            ON CONFLICT (processor_name)
            DO UPDATE SET
                source_table = EXCLUDED.source_table,
                cursor_mode = EXCLUDED.cursor_mode,
                last_id = EXCLUDED.last_id,
                last_timestamp = EXCLUDED.last_timestamp,
                last_batch_count = 0,
                last_status = 'IDLE',
                notes = EXCLUDED.notes,
                updated_at = NOW()
            """
        ),
        {
            "processor_name": processor_name,
            "source_table": source_table,
            "last_id": _checkpoint_safe_last_id(start_source_id),
            "last_timestamp": start_timestamp,
            "notes": _build_notes(
                f"Seeded for latest {WINDOW} rows rebuild",
                source_id_text=start_source_id,
            ),
        },
    )
    db.commit()


def run_processor(label, processor, *, limit, batch_size, start_source_id=None):
    print("\n" + "=" * 80)
    print(f"Running: {label}")
    print("=" * 80)
    print(
        f"limit={limit}, start_source_id={start_source_id}, "
        f"batch_size={batch_size}"
    )
    result = processor.run(
        limit=limit,
        start_source_id=start_source_id,
        batch_size=batch_size,
    )
    print("Result:")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return result


def main():
    db_gen = get_db()
    db = next(db_gen)

    try:
        id_jobs = [
            {
                "label": "user_account_sync",
                "processor": UserAccountSync(db),
                "table": "staging_user_account",
                "batch_size": 2000,
            },
            {
                "label": "lead_sync",
                "processor": LeadSync(db),
                "table": "staging_lead_tracking",
                "batch_size": 2000,
            },
            {
                "label": "booking_confirm_sync",
                "processor": BookingConfirmSync(db),
                "table": "staging_booking_confirm",
                "batch_size": 2000,
            },
            {
                "label": "user_contact_info_sync",
                "processor": UserContactInfoSync(db),
                "table": "staging_user_contact_info",
                "batch_size": 2000,
            },
            {
                "label": "checkin_form_sync",
                "processor": CheckinFormSync(db),
                "table": "staging_checkin_form",
                "batch_size": 1500,
            },
            {
                "label": "checkout_form_sync",
                "processor": CheckoutFormSync(db),
                "table": "staging_checkout_form",
                "batch_size": 1500,
            },
            {
                "label": "email_message_sync",
                "processor": EmailMessageSync(db),
                "table": "staging_email_messages",
                "batch_size": 1500,
            },
            {
                "label": "booking_audit_history_sync",
                "processor": BookingAuditHistorySync(db),
                "table": "staging_booking_audit_history",
                "batch_size": 2000,
            },
        ]

        time_jobs = [
            {
                "label": "call_log_sync",
                "processor": CallLogSync(db),
                "table": "staging_call_log_unified",
                "processor_name": "call_log_sync",
                "source_table": f'"{SCHEMA}".staging_call_log_unified',
                "ts_expr": "COALESCE(updated_at, synced_at)",
                "id_expr": "source_id",
                "batch_size": 2000,
            },
            {
                "label": "whatsapp_message_sync",
                "processor": WhatsAppMessageSync(db),
                "table": "staging_whatsapp_messages",
                "processor_name": "whatsapp_message_sync",
                "source_table": f'"{SCHEMA}".staging_whatsapp_messages',
                "ts_expr": "COALESCE(message_time, synced_at)",
                "id_expr": "source_id",
                "batch_size": 2000,
            },
            {
                "label": "user_ticket_sync",
                "processor": UserTicketSync(db),
                "table": "staging_user_ticket",
                "processor_name": "user_ticket_sync",
                "source_table": f'"{SCHEMA}".staging_user_ticket',
                "ts_expr": "COALESCE(close_date, created_at, synced_at)",
                "id_expr": "source_id",
                "batch_size": 1500,
            },
            {
                "label": "booking_invoice_details_sync",
                "processor": BookingInvoiceDetailsSync(db),
                "table": "staging_booking_invoice_details",
                "processor_name": "booking_invoice_details_sync",
                "source_table": f'"{SCHEMA}".staging_booking_invoice_details',
                "ts_expr": "COALESCE(utr_added_on, send_time, created_on, synced_at)",
                "id_expr": "source_id",
                "batch_size": 1500,
            },
            {
                "label": "user_wishlist_sync",
                "processor": UserWishlistSync(db),
                "table": "staging_user_wishlist",
                "processor_name": "user_wishlist_sync",
                "source_table": f'"{SCHEMA}".staging_user_wishlist',
                "ts_expr": "COALESCE(added_on, synced_at)",
                "id_expr": "source_id",
                "batch_size": 2000,
            },
        ]

        print("=" * 80)
        print("Computing latest-window cursors")
        print("=" * 80)

        id_starts = {}
        for job in id_jobs:
            start_id = get_latest_id_start(db, job["table"], WINDOW)
            id_starts[job["label"]] = start_id
            print(f'{job["label"]}: table={job["table"]}, start_source_id={start_id}')

        time_starts = {}
        for job in time_jobs:
            start_id, start_ts = get_latest_time_cursor_start(
                db=db,
                table_name=job["table"],
                ts_expr=job["ts_expr"],
                id_expr=job["id_expr"],
                window=WINDOW,
            )
            time_starts[job["label"]] = (start_id, start_ts)
            print(
                f'{job["label"]}: table={job["table"]}, '
                f'start_source_id={start_id}, start_timestamp={start_ts}'
            )

        print("\n" + "=" * 80)
        print(f"Running AnalyticsEngine rebuild for latest ~{WINDOW} rows")
        print("=" * 80)

        ordered_id_labels = [
            "user_account_sync",
            "lead_sync",
            "booking_confirm_sync",
            "user_contact_info_sync",
            "checkin_form_sync",
            "checkout_form_sync",
            "email_message_sync",
            "booking_audit_history_sync",
        ]

        id_lookup = {job["label"]: job for job in id_jobs}
        for label in ordered_id_labels:
            job = id_lookup[label]
            run_processor(
                label=job["label"],
                processor=job["processor"],
                limit=WINDOW,
                batch_size=job["batch_size"],
                start_source_id=id_starts[job["label"]],
            )

        for job in time_jobs:
            start_id, start_ts = time_starts[job["label"]]
            seed_time_cursor_checkpoint(
                db=db,
                processor_name=job["processor_name"],
                source_table=job["source_table"],
                start_source_id=start_id,
                start_timestamp=start_ts,
            )

        ordered_time_labels = [
            "call_log_sync",
            "whatsapp_message_sync",
            "user_ticket_sync",
            "booking_invoice_details_sync",
            "user_wishlist_sync",
        ]

        time_lookup = {job["label"]: job for job in time_jobs}
        for label in ordered_time_labels:
            job = time_lookup[label]
            start_id, _ = time_starts[job["label"]]
            run_processor(
                label=job["label"],
                processor=job["processor"],
                limit=WINDOW,
                batch_size=job["batch_size"],
                start_source_id=start_id,
            )

        print("\nDone.")

    finally:
        try:
            db.close()
        except Exception:
            pass
        try:
            next(db_gen, None)
        except Exception:
            pass


if __name__ == "__main__":
    main()


