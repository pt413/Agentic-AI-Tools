from ..processors.base_sync_processor import BaseSyncProcessor
from ..core.service_container import ServiceContainer
from ..core.config import STAGING_TRAVEL_CART
from ..processors.sync_support import append_contexts


class TravelCartSync(BaseSyncProcessor):
    PROCESSOR_NAME = "travel_cart_sync"
    SOURCE_TABLE = STAGING_TRAVEL_CART
    SOURCE_TABLE_NAME = "staging_travel_cart"

    def __init__(self, db):
        super().__init__(db, ServiceContainer(db), default_batch_size=5000)

    def fetch_rows(self, last_source_id: int, batch_size: int):
        return self.source.fetch_travel_cart(last_source_id=last_source_id, batch_size=batch_size)

    def _resolve_event_name(self, row):
        if getattr(row, "advance_amount", None) not in (None, 0, 0.0, "0", "0.0"):
            return "travel_cart_checkout_initiated"
        if getattr(row, "total_amount", None) not in (None, 0, 0.0, "0", "0.0"):
            return "travel_cart_added"
        return "travel_cart_created"

    def process_row(self, r):
        event_time = self.source.extract_travel_cart_event_time(r)
        if not event_time:
            return {"processed": 1, "last_source_id": r.source_id}

        event_name = self._resolve_event_name(r)
        event_meta = {
            "source_id": getattr(r, "source_id", None),
            "user_id": getattr(r, "user_id", None),
            "property_id": getattr(r, "prop_id", None),
            "travel_from_date": getattr(r, "travel_from_date", None),
            "travel_to_date": getattr(r, "travel_to_date", None),
            "nights": getattr(r, "nights", None),
            "booking_type": getattr(r, "booking_type", None),
            "advance_amount": getattr(r, "advance_amount", None),
            "pending_amount": getattr(r, "pending_amount", None),
            "source": getattr(r, "source", None),
            "bkc_status": getattr(r, "bkc_status", None),
        }
        event_meta = {k: v for k, v in event_meta.items() if v is not None}

        metric_value = getattr(r, "total_amount", None)
        event_id = self.events.create_event(
            event_family="intent",
            event_name=event_name,
            event_channel="travel_cart",
            event_direction=None,
            event_time=event_time,
            event_end_time=None,
            event_status=str(getattr(r, "bkc_status", None)) if getattr(r, "bkc_status", None) is not None else None,
            source_table=self.SOURCE_TABLE_NAME,
            source_id=str(r.source_id),
            metric_value=metric_value,
            metric_unit=None,
            metric_name="cart_value" if metric_value is not None else None,
            meta=event_meta,
        )

        participants_written = 0
        contexts_written = 0
        created_events = 1 if event_id else 0

        if event_id:
            seq = 1

            user_id = getattr(r, "user_id", None)
            if user_id not in (None, ""):
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="user_id",
                    key_value=str(user_id),
                    role="customer",
                    direction_role=None,
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                if participant_id:
                    participants_written += 1
                    seq += 1

            contexts_written += append_contexts(self, event_id, self.source.extract_travel_cart_contexts(r))

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
            "processed_travel_cart_rows": result["processed_rows"],
            "created_events": result.get("created_events", 0),
            "contexts_written": result.get("contexts_written", 0),
            "last_source_id": result["last_source_id"],
        }

