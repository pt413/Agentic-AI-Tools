from ..processors.time_cursor_sync_processor import TimeCursorSyncProcessor
from ..core.service_container import ServiceContainer
from ..core.config import STAGING_BOOKING_INVOICE
from ..processors.sync_support import add_staff_participant, append_contexts, slugify_label


class BookingInvoiceDetailsSync(TimeCursorSyncProcessor):
    PROCESSOR_NAME = "booking_invoice_details_sync"
    SOURCE_TABLE = STAGING_BOOKING_INVOICE
    SOURCE_TABLE_NAME = "staging_booking_invoice_details"

    def __init__(self, db):
        super().__init__(db, ServiceContainer(db), default_batch_size=3000)

    def fetch_rows_since(self, last_timestamp, last_source_id, batch_size: int):
        return self.source.fetch_booking_invoice_details_since(
            last_timestamp=last_timestamp,
            last_source_id=last_source_id,
            batch_size=batch_size,
        )
    def fetch_rows_with_time_cursor(self, last_source_id, last_timestamp, batch_size: int):
        return self.source.fetch_booking_invoice_details(
            last_source_id=last_source_id,
            last_timestamp=last_timestamp,
            batch_size=batch_size,
        )
    def get_row_cursor_timestamp(self, row, row_result=None):
        return self.source.extract_booking_invoice_cursor_time(row)

    def _base_meta(self, row):
        meta = {
            "source_id": getattr(row, "source_id", None),
            "booking_id": getattr(row, "booking_id", None),
            "payment_id": getattr(row, "payment_id", None),
            "lead_id": getattr(row, "booking_lead_id", None),
            "property_id": getattr(row, "booking_prop_id", None),
            "user_id": getattr(row, "booking_user_id", None),
            "booking_status": getattr(row, "booking_status", None),
            "amount_status": getattr(row, "amount_status", None),
            "duration_period": getattr(row, "duration_period", None),
            "mail_status": getattr(row, "mail_status", None),
            "sa_mail_status": getattr(row, "sa_mail_status", None),
            "reminder_mail": getattr(row, "reminder_mail", None),
            "amount_recieved": getattr(row, "amount_recieved", None),
            "amount": getattr(row, "amount", None),
            "total_amount": getattr(row, "total_amount", None),
            "disc": getattr(row, "disc", None),
            "pending_balance": getattr(row, "pending_balance", None),
            "payment_mode": getattr(row, "payment_mode", None),
            "comment": getattr(row, "comment", None),
            "status": getattr(row, "status", None),
            "mail_count": getattr(row, "mail_count", None),
            "modify_flag": getattr(row, "modify_flag", None),
            "transaction_type": getattr(row, "transaction_type", None),
            "utr_no": getattr(row, "utr_no", None),
            "utr_added_by": getattr(row, "utr_added_by", None),
        }
        return {k: v for k, v in meta.items() if v is not None}

    def _amount_value(self, row):
        return (
            getattr(row, "amount_recieved", None)
            or getattr(row, "total_amount", None)
            or getattr(row, "amount", None)
        )

    def _create_invoice_event(self, row, *, event_name, event_family, event_time, metric_name):
        metric_value = self._amount_value(row)

        event_meta = {
            "source_id": getattr(row, "source_id", None),
            "booking_id": getattr(row, "booking_id", None),
            "payment_id": getattr(row, "payment_id", None),
            "lead_id": getattr(row, "booking_lead_id", None),
            "property_id": getattr(row, "booking_prop_id", None),
            "user_id": getattr(row, "booking_user_id", None),
            "booking_status": getattr(row, "booking_status", None),
            "amount_status": getattr(row, "amount_status", None),
            "duration_period": getattr(row, "duration_period", None),
            "mail_status": getattr(row, "mail_status", None),
            "sa_mail_status": getattr(row, "sa_mail_status", None),
            "reminder_mail": getattr(row, "reminder_mail", None),
            "amount_recieved": getattr(row, "amount_recieved", None),
            "amount": getattr(row, "amount", None),
            "total_amount": getattr(row, "total_amount", None),
            "disc": getattr(row, "disc", None),
            "pending_balance": getattr(row, "pending_balance", None),
            "payment_mode": getattr(row, "payment_mode", None),
            "status": getattr(row, "status", None),
            "mail_count": getattr(row, "mail_count", None),
            "transaction_type": getattr(row, "transaction_type", None),
            "utr_no": getattr(row, "utr_no", None),
            "utr_added_by": getattr(row, "utr_added_by", None),
            "utr_added_on": getattr(row, "utr_added_on", None),
            "send_time": getattr(row, "send_time", None),
            "created_on": getattr(row, "created_on", None),
        }
        event_meta = {k: v for k, v in event_meta.items() if v is not None}

        event_id = self.events.create_event(
            event_family=event_family,
            event_name=event_name,
            event_channel="booking",
            event_direction=None,
            event_time=event_time,
            event_end_time=None,
            event_status=None,
            source_table=self.SOURCE_TABLE_NAME,
            source_id=str(row.source_id),
            metric_value=metric_value,
            metric_unit=None,
            metric_name=metric_name,
            meta=event_meta,
        )

        participants_written = 0
        contexts_written = 0

        if event_id:
            seq = 1

            booking_user_id = getattr(row, "booking_user_id", None)
            if booking_user_id not in (None, "", 0, "0"):
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="user_id",
                    key_value=str(booking_user_id),
                    role="customer",
                    direction_role="from",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=row.source_id,
                    event_time=event_time,
                )
                participants_written += 1 if participant_id else 0
                seq += 1

            seq, added = add_staff_participant(
                self,
                event_id=event_id,
                participant_seq=seq,
                staff_ref=getattr(row, "utr_added_by", None),
                role="utr_added_by",
                source_table=self.SOURCE_TABLE_NAME,
                source_id=row.source_id,
                event_time=event_time,
            )
            participants_written += added

            contexts_written += append_contexts(
                self,
                event_id,
                self.source.extract_booking_invoice_contexts(row),
            )

        return event_id, participants_written, contexts_written
    def process_row(self, r):
        created_events = 0
        participants_written = 0
        contexts_written = 0

        event_specs = []
        if getattr(r, "created_on", None):
            event_specs.append(("invoice_created", "invoice", getattr(r, "created_on", None), "invoice_amount"))
        if getattr(r, "send_time", None):
            event_specs.append(("invoice_sent", "invoice", getattr(r, "send_time", None), "invoice_amount"))
        if getattr(r, "utr_added_on", None):
            event_specs.append(("invoice_utr_added", "payment", getattr(r, "utr_added_on", None), "payment_amount"))

        status_value = getattr(r, "status", None)
        if status_value:
            mapped_name = {
                "closed": "invoice_closed",
                "defaulted": "invoice_defaulted",
                "cancel": "invoice_cancelled",
                "cancel_settle": "invoice_cancelled",
                "cancel_and_settle": "invoice_cancelled",
            }.get(slugify_label(status_value), "invoice_status_updated")
            status_time = self.source.extract_booking_invoice_cursor_time(r)
            if status_time:
                event_specs.append((mapped_name, "invoice", status_time, "invoice_amount"))

        for event_name, event_family, event_time, metric_name in event_specs:
            event_id, p_written, c_written = self._create_invoice_event(
                r,
                event_name=event_name,
                event_family=event_family,
                event_time=event_time,
                metric_name=metric_name,
            )
            created_events += 1 if event_id else 0
            participants_written += p_written
            contexts_written += c_written

        return {
            "processed": 1,
            "cursor_source_id": r.source_id,
            "created_events": created_events,
            "participants_written": participants_written,
            "contexts_written": contexts_written,
        }

    def run(self, batch_size=3000, limit=None, start_source_id=None):
        result = super().run(batch_size=batch_size, limit=limit, start_source_id=start_source_id)
        return {
            "processed_booking_invoice_rows": result["processed_rows"],
            "created_events": result.get("created_events", 0),
            "participants_written": result.get("participants_written", 0),
            "contexts_written": result.get("contexts_written", 0),
            "last_source_id": result["last_source_id"],
        }

