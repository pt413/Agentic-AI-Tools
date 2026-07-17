import json

from sqlalchemy import text

from ..core.config import BOOKING_FACT


class BookingFactService:
    def __init__(self, db):
        self.db = db
        self._sql_upsert = text(
            f"""
            INSERT INTO {BOOKING_FACT}
            (
                event_id,
                source_table,
                source_id,
                booking_id,
                lead_id,
                property_id,
                customer_phone,
                sales_phone,
                executive_ref,
                booking_status,
                booking_amount,
                currency_code,
                booking_time,
                raw_payload
            )
            VALUES
            (
                :event_id,
                :source_table,
                :source_id,
                :booking_id,
                :lead_id,
                :property_id,
                :customer_phone,
                :sales_phone,
                :executive_ref,
                :booking_status,
                :booking_amount,
                :currency_code,
                :booking_time,
                CAST(:raw_payload AS JSONB)
            )
            ON CONFLICT (source_table, source_id)
            DO UPDATE
            SET
                event_id = COALESCE(EXCLUDED.event_id, {BOOKING_FACT}.event_id),
                booking_id = COALESCE(EXCLUDED.booking_id, {BOOKING_FACT}.booking_id),
                lead_id = COALESCE(EXCLUDED.lead_id, {BOOKING_FACT}.lead_id),
                property_id = COALESCE(EXCLUDED.property_id, {BOOKING_FACT}.property_id),
                customer_phone = COALESCE(EXCLUDED.customer_phone, {BOOKING_FACT}.customer_phone),
                sales_phone = COALESCE(EXCLUDED.sales_phone, {BOOKING_FACT}.sales_phone),
                executive_ref = COALESCE(EXCLUDED.executive_ref, {BOOKING_FACT}.executive_ref),
                booking_status = COALESCE(EXCLUDED.booking_status, {BOOKING_FACT}.booking_status),
                booking_amount = COALESCE(EXCLUDED.booking_amount, {BOOKING_FACT}.booking_amount),
                currency_code = COALESCE(EXCLUDED.currency_code, {BOOKING_FACT}.currency_code),
                booking_time = COALESCE(EXCLUDED.booking_time, {BOOKING_FACT}.booking_time),
                raw_payload = CASE
                    WHEN EXCLUDED.raw_payload IS NOT NULL THEN EXCLUDED.raw_payload
                    ELSE {BOOKING_FACT}.raw_payload
                END,
                updated_at = NOW()
            RETURNING booking_fact_id
            """
        )

    def upsert_booking_fact(
        self,
        event_id,
        source_table,
        source_id,
        booking_id=None,
        lead_id=None,
        property_id=None,
        customer_phone=None,
        sales_phone=None,
        executive_ref=None,
        booking_status=None,
        booking_amount=None,
        currency_code=None,
        booking_time=None,
        raw_payload=None,
    ):
        raw_payload_json = json.dumps(raw_payload or {}, default=str)
        row = self.db.execute(
            self._sql_upsert,
            {
                "event_id": event_id,
                "source_table": source_table,
                "source_id": str(source_id),
                "booking_id": str(booking_id) if booking_id is not None else None,
                "lead_id": str(lead_id) if lead_id is not None else None,
                "property_id": str(property_id) if property_id is not None else None,
                "customer_phone": customer_phone,
                "sales_phone": sales_phone,
                "executive_ref": executive_ref,
                "booking_status": booking_status,
                "booking_amount": booking_amount,
                "currency_code": currency_code,
                "booking_time": booking_time,
                "raw_payload": raw_payload_json,
            },
        ).fetchone()
        return row.booking_fact_id if row else None
