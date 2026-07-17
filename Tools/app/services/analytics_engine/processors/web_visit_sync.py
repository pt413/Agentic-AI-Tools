from ..processors.base_sync_processor import BaseSyncProcessor
from ..core.service_container import ServiceContainer
from ..core.config import STAGING_WEB_VISITS
from ..processors.sync_support import append_contexts


class WebVisitSync(BaseSyncProcessor):
    PROCESSOR_NAME = "web_visit_sync"
    SOURCE_TABLE = STAGING_WEB_VISITS
    SOURCE_TABLE_NAME = "staging_web_visits"

    def __init__(self, db):
        super().__init__(db, ServiceContainer(db), default_batch_size=5000)

    def fetch_rows(self, last_source_id: int, batch_size: int):
        return self.source.fetch_web_visits(last_source_id=last_source_id, batch_size=batch_size)

    def process_row(self, r):
        event_time = self.source.extract_web_visit_event_time(r)
        if not event_time:
            return {"processed": 1, "last_source_id": r.source_id}

        event_meta = {
            "source_id": getattr(r, "source_id", None),
            "referal_page": getattr(r, "referal_page", None),
            "current_page": getattr(r, "current_page", None),
            "ip_address": getattr(r, "ip_address", None),
            "session_id": getattr(r, "session_id", None),
            "user_id": getattr(r, "user_id", None),
            "lead_id": getattr(r, "lead_id", None),
            "property_id": getattr(r, "prop_id", None),
            "source": getattr(r, "source", None),
            "user_agent": getattr(r, "user_agent", None),
        }
        event_meta = {k: v for k, v in event_meta.items() if v is not None}

        event_id = self.events.create_event(
            event_family="web",
            event_name="web_page_view",
            event_channel="web",
            event_direction=None,
            event_time=event_time,
            event_end_time=None,
            event_status=None,
            source_table=self.SOURCE_TABLE_NAME,
            source_id=str(r.source_id),
            metric_value=1,
            metric_unit="count",
            metric_name="page_view",
            meta=event_meta,
        )

        contexts_written = 0
        created_events = 1 if event_id else 0
        if event_id:
            contexts_written += append_contexts(self, event_id, self.source.extract_web_visit_contexts(r))

        return {
            "processed": 1,
            "last_source_id": r.source_id,
            "created_events": created_events,
            "contexts_written": contexts_written,
        }

    def run(self, batch_size=5000, limit=None, start_source_id=None):
        result = super().run(batch_size=batch_size, limit=limit, start_source_id=start_source_id)
        return {
            "processed_web_visit_rows": result["processed_rows"],
            "created_events": result.get("created_events", 0),
            "contexts_written": result.get("contexts_written", 0),
            "last_source_id": result["last_source_id"],
        }

