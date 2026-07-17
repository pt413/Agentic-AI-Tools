from ..processors.base_sync_processor import BaseSyncProcessor
from ..core.service_container import ServiceContainer
from ..core.config import STAGING_CHECKIN
from ..processors.sync_support import add_email_participant, add_staff_participant, append_contexts


class CheckinFormSync(BaseSyncProcessor):
    PROCESSOR_NAME = "checkin_form_sync"
    SOURCE_TABLE = STAGING_CHECKIN
    SOURCE_TABLE_NAME = "staging_checkin_form"

    def __init__(self, db):
        super().__init__(db, ServiceContainer(db), default_batch_size=3000)

    def fetch_rows(self, last_source_id: int, batch_size: int):
        return self.source.fetch_checkin_forms(last_source_id=last_source_id, batch_size=batch_size)

    def process_row(self, r):
        event_time = self.source.extract_checkin_event_time(r)
        if not event_time:
            return {"processed": 1, "last_source_id": r.source_id}

        event_meta = {
            "source_id": getattr(r, "source_id", None),
            "booking_id": getattr(r, "booking_id", None),
            "property_id": getattr(r, "prop_id", None),
            "property_name": getattr(r, "prop_name", None),
            "stay_rating": getattr(r, "stay_rating", None),
            "sales_rating": getattr(r, "sales_rating", None),
            "cleaning_rating": getattr(r, "cleaning_rating", None),
            "welcome_flag": getattr(r, "welcome_flag", None),
            "linen_flag": getattr(r, "linen_flag", None),
            "sales_comment": getattr(r, "sales_comment", None),
            "stay_comment": getattr(r, "stay_comment", None),
            "welcome_comment": getattr(r, "welcome_comment", None),
            "suggestions": getattr(r, "suggestions", None),
            "other_comment": getattr(r, "other_comment", None),
        }
        event_meta = {k: v for k, v in event_meta.items() if v is not None}

        metric_value = getattr(r, "stay_rating", None)
        event_id = self.events.create_event(
            event_family="stay",
            event_name="checkin_completed",
            event_channel="checkin",
            event_direction=None,
            event_time=event_time,
            event_end_time=None,
            event_status=None,
            source_table=self.SOURCE_TABLE_NAME,
            source_id=str(r.source_id),
            metric_value=metric_value,
            metric_unit=None,
            metric_name="stay_rating",
            meta=event_meta,
        )

        participants_written = 0
        contexts_written = 0
        created_events = 1 if event_id else 0

        if event_id:
            participant_data = self.source.extract_checkin_participants(r)

            guest_email = participant_data.get("user_email")
            if guest_email:
                self.identity.resolve_or_create_person_from_keys(
                    candidate_keys=[("email", guest_email)],
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=str(r.source_id),
                    event_time=event_time,
                    seed_fields={
                        "primary_email": guest_email,
                        "person_kind": "external",
                        "kind_confidence": 1,
                    },
                    merge_reason="checkin_guest_seed",
                )

            seen_staff_refs = set()
            for role_key in ("supervisor", "caretaker", "ops_manager", "salesperson"):
                raw_staff_ref = participant_data.get(role_key)
                if raw_staff_ref in (None, ""):
                    continue

                staff_ref = str(raw_staff_ref).strip()
                if not staff_ref or staff_ref in seen_staff_refs:
                    continue
                seen_staff_refs.add(staff_ref)

                self.identity.resolve_or_create_person_from_keys(
                    candidate_keys=[("staff_ref", staff_ref)],
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=str(r.source_id),
                    event_time=event_time,
                    seed_fields={
                        "canonical_name": staff_ref,
                        "person_kind": "internal",
                        "kind_confidence": 1,
                    },
                    merge_reason=f"checkin_{role_key}_seed",
                )

            seq = 1
            seq, added = add_email_participant(
                self,
                event_id=event_id,
                participant_seq=seq,
                email=guest_email,
                role="guest_email",
                direction_role="from",
                source_table=self.SOURCE_TABLE_NAME,
                source_id=r.source_id,
                event_time=event_time,
            )
            participants_written += added

            for role_key in ("supervisor", "caretaker", "ops_manager", "salesperson"):
                seq, added = add_staff_participant(
                    self,
                    event_id=event_id,
                    participant_seq=seq,
                    staff_ref=participant_data.get(role_key),
                    role=role_key,
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                participants_written += added

            contexts_written += append_contexts(
                self,
                event_id,
                self.source.extract_checkin_contexts(r),
            )

        return {
            "processed": 1,
            "last_source_id": r.source_id,
            "created_events": created_events,
            "participants_written": participants_written,
            "contexts_written": contexts_written,
        }
    def run(self, batch_size=3000, limit=None, start_source_id=None):
        result = super().run(batch_size=batch_size, limit=limit, start_source_id=start_source_id)
        return {
            "processed_checkin_rows": result["processed_rows"],
            "created_events": result.get("created_events", 0),
            "participants_written": result.get("participants_written", 0),
            "contexts_written": result.get("contexts_written", 0),
            "last_source_id": result["last_source_id"],
        }

