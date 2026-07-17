from ..processors.base_sync_processor import BaseSyncProcessor
from ..core.service_container import ServiceContainer
from ..core.config import STAGING_BOOKING_AUDIT
from ..processors.sync_support import add_staff_participant, append_contexts


class BookingAuditHistorySync(BaseSyncProcessor):
    PROCESSOR_NAME = "booking_audit_history_sync"
    SOURCE_TABLE = STAGING_BOOKING_AUDIT
    SOURCE_TABLE_NAME = "staging_booking_audit_history"

    def __init__(self, db):
        super().__init__(db, ServiceContainer(db), default_batch_size=5000)

    def fetch_rows(self, last_source_id: int, batch_size: int):
        return self.source.fetch_booking_audit_history(last_source_id=last_source_id, batch_size=batch_size)

    def process_row(self, r):
        event_time = self.source.extract_booking_audit_event_time(r)
        if not event_time:
            return {"processed": 1, "last_source_id": r.source_id}

        event_meta = {
            "source_id": getattr(r, "source_id", None),
            "booking_id": getattr(r, "booking_id", None),
            "updated_by": getattr(r, "updated_by", None),
            "audit_history": getattr(r, "audit_history", None),
            "booking_status": getattr(r, "booking_status", None),
            "lead_id": getattr(r, "booking_lead_id", None),
            "property_id": getattr(r, "booking_prop_id", None),
            "user_id": getattr(r, "booking_user_id", None),
        }
        event_meta = {k: v for k, v in event_meta.items() if v is not None}

        event_id = self.events.create_event(
            event_family="booking",
            event_name="booking_audit_updated",
            event_channel="booking",
            event_direction=None,
            event_time=event_time,
            event_end_time=None,
            event_status=getattr(r, "booking_status", None),
            source_table=self.SOURCE_TABLE_NAME,
            source_id=str(r.source_id),
            metric_value=None,
            metric_unit=None,
            metric_name=None,
            meta=event_meta,
        )

        participants_written = 0
        contexts_written = 0
        created_events = 1 if event_id else 0
        if event_id:
            seq = 1
            seq, added = add_staff_participant(
                self,
                event_id=event_id,
                participant_seq=seq,
                staff_ref=getattr(r, "updated_by", None),
                role="updated_by",
                source_table=self.SOURCE_TABLE_NAME,
                source_id=r.source_id,
                event_time=event_time,
            )
            participants_written += added
            contexts_written += append_contexts(self, event_id, self.source.extract_booking_audit_contexts(r))

        return {
            "processed": 1,
            "last_source_id": r.source_id,
            "created_events": created_events,
            "participants_written": participants_written,
            "contexts_written": contexts_written,
        }

    def run(self, batch_size=5000, limit=None, start_source_id=None):
        result = super().run(batch_size=batch_size, limit=limit, start_source_id=start_source_id)
        return {
            "processed_booking_audit_rows": result["processed_rows"],
            "created_events": result.get("created_events", 0),
            "participants_written": result.get("participants_written", 0),
            "contexts_written": result.get("contexts_written", 0),
            "last_source_id": result["last_source_id"],
        }

