from sqlalchemy import text

from ..processors.base_sync_processor import BaseSyncProcessor
from ..core.service_container import ServiceContainer
from ..core.config import STAGING_SITE_VISITS, STAGING_LEADS
from ..processors.sync_support import (
    add_staff_participant,
    add_phone_participant,
    add_email_participant,
    append_contexts,
)


class SiteVisitSync(BaseSyncProcessor):
    PROCESSOR_NAME = "site_visit_sync"
    SOURCE_TABLE = STAGING_SITE_VISITS
    SOURCE_TABLE_NAME = "staging_site_visits"

    def __init__(self, db):
        super().__init__(db, ServiceContainer(db), default_batch_size=5000)

    def fetch_rows(self, last_source_id: int, batch_size: int):
        return self.source.fetch_site_visits(last_source_id=last_source_id, batch_size=batch_size)

    def _resolve_event_name(self, schedule_status):
        try:
            status = int(schedule_status) if schedule_status is not None else None
        except Exception:
            status = None

        if status == 0:
            return "site_visit_done", "completed"
        if status == 1:
            return "site_visit_scheduled", "scheduled"
        if status == 2:
            return "site_visit_cancelled", "cancelled"
        return "site_visit_updated", None

    def _fetch_lead_row(self, lead_id):
        if lead_id in (None, ""):
            return None

        try:
            lead_id = int(lead_id)
        except Exception:
            return None

        return self.db.execute(
            text(
                f"""
                SELECT
                    source_id,
                    user_id,
                    person_id,
                    email,
                    contact_number,
                    contact_number_alt
                FROM {STAGING_LEADS}
                WHERE source_id = :lead_id
                LIMIT 1
                """
            ),
            {"lead_id": lead_id},
        ).mappings().fetchone()

    def process_row(self, r):
        event_time = self.source.extract_site_visit_event_time(r)
        if not event_time:
            return {"processed": 1, "last_source_id": r.source_id}

        event_name, event_status = self._resolve_event_name(getattr(r, "schedule_status", None))

        executive_ref = self.source.clean_actor_ref(getattr(r, "executive_id", None))
        executive_raw = self.source.normalize_actor_ref(getattr(r, "executive_id", None))

        event_meta = {
            "source_id": getattr(r, "source_id", None),
            "lead_id": getattr(r, "lead_id", None),
            "executive_id": executive_ref,
            "executive_id_raw": executive_raw,
            "executive_id_mode": "system" if executive_raw and self.source.is_system_actor(executive_raw) else "actor" if executive_raw else None,
            "building_id": getattr(r, "building_id", None),
            "property_id": getattr(r, "prop_id", None),
            "unit_type": getattr(r, "unit_type", None),
            "visit_type": getattr(r, "visit_type", None),
            "schedule_status": getattr(r, "schedule_status", None),
            "site_visit_date": getattr(r, "site_visit_date", None),
            "added_on": getattr(r, "added_on", None),
        }
        event_meta = {k: v for k, v in event_meta.items() if v is not None}

        event_id = self.events.create_event(
            event_family="engagement",
            event_name=event_name,
            event_channel="site_visit",
            event_direction=None,
            event_time=event_time,
            event_end_time=None,
            event_status=event_status,
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
                staff_ref=executive_ref,
                role="executive",
                source_table=self.SOURCE_TABLE_NAME,
                source_id=r.source_id,
                event_time=event_time,
            )
            participants_written += added

            lead_row = self._fetch_lead_row(getattr(r, "lead_id", None))
            lead_user_id = lead_row.get("user_id") if lead_row else None
            lead_email = lead_row.get("email") if lead_row else None
            lead_phone = lead_row.get("contact_number") if lead_row else None
            lead_alt_phone = lead_row.get("contact_number_alt") if lead_row else None

            if lead_user_id not in (None, ""):
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="user_id",
                    key_value=str(lead_user_id),
                    role="customer",
                    direction_role=None,
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                if participant_id:
                    participants_written += 1
                    seq += 1
            else:
                seq, added = add_email_participant(
                    self,
                    event_id=event_id,
                    participant_seq=seq,
                    email=lead_email,
                    role="customer",
                    direction_role=None,
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                participants_written += added

                if added == 0:
                    seq, added = add_phone_participant(
                        self,
                        event_id=event_id,
                        participant_seq=seq,
                        phone=lead_phone,
                        role="customer",
                        direction_role=None,
                        source_table=self.SOURCE_TABLE_NAME,
                        source_id=r.source_id,
                        event_time=event_time,
                    )
                    participants_written += added

                if added == 0:
                    seq, added = add_phone_participant(
                        self,
                        event_id=event_id,
                        participant_seq=seq,
                        phone=lead_alt_phone,
                        role="customer",
                        direction_role=None,
                        source_table=self.SOURCE_TABLE_NAME,
                        source_id=r.source_id,
                        event_time=event_time,
                    )
                    participants_written += added

            contexts_written += append_contexts(
                self,
                event_id,
                self.source.extract_site_visit_contexts(r),
            )
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
            "processed_site_visit_rows": result["processed_rows"],
            "created_events": result.get("created_events", 0),
            "participants_written": result.get("participants_written", 0),
            "contexts_written": result.get("contexts_written", 0),
            "last_source_id": result["last_source_id"],
        }
