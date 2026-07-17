from ..processors.base_sync_processor import BaseSyncProcessor
from ..core.service_container import ServiceContainer
from ..core.config import STAGING_USER_CONTACT_INFO


class UserContactInfoSync(BaseSyncProcessor):
    PROCESSOR_NAME = "user_contact_info_sync"
    SOURCE_TABLE = STAGING_USER_CONTACT_INFO
    SOURCE_TABLE_NAME = "staging_user_contact_info"

    def __init__(self, db):
        super().__init__(db, ServiceContainer(db), default_batch_size=5000)

    def fetch_rows(self, last_source_id: int, batch_size: int):
        return self.source.fetch_user_contact_info(
            last_source_id=last_source_id,
            batch_size=batch_size,
        )

    def process_row(self, r):
        event_time = self.source.extract_user_contact_info_event_time(r)
        if not event_time:
            return {
                "processed": 1,
                "last_source_id": r.source_id,
            }

        user_id = getattr(r, "user_id", None)
        email = getattr(r, "email", None)
        mobile = getattr(r, "normalized_mobile", None) or getattr(r, "mobile", None)
        contact_name = getattr(r, "contact_name", None)

        candidate_keys = []
        if user_id not in (None, ""):
            candidate_keys.append(("user_id", str(user_id)))
        if email:
            candidate_keys.append(("email", str(email)))
        if mobile:
            # The current identity_person_key.chk_key_type constraint allows phone,
            # but not whatsapp_number. Store mobile once as phone; timeline matching
            # uses phone/mobile to include both calls and WhatsApp conversations.
            candidate_keys.append(("phone", str(mobile)))

        seed_fields = {
            "primary_phone": mobile,
            "primary_email": email,
            "canonical_name": contact_name,
            "person_kind": "external" if candidate_keys else "unknown",
            "kind_confidence": 1 if candidate_keys else 0,
        }

        event_meta = self.source.build_user_contact_info_event_meta(r)
        person_id = None
        merged_people = 0
        repaired_participants = 0

        if candidate_keys:
            result = self.identity.resolve_or_create_person_from_keys(
                candidate_keys=candidate_keys,
                source_table=self.SOURCE_TABLE_NAME,
                source_id=str(r.source_id),
                event_time=event_time,
                seed_fields=seed_fields,
                merge_reason="booking_contact_bridge",
                return_details=True,
            )
            person_id = result["person_id"] if isinstance(result, dict) else result
            merged_people = int(result.get("merged_people", 0)) if isinstance(result, dict) else 0

            if person_id:
                repaired_participants = self.identity.reassign_event_participants_for_keys(
                    person_id=person_id,
                    candidate_keys=candidate_keys,
                )
                event_meta["person_id"] = person_id

        event_id = self.events.create_event(
            event_family="identity",
            event_name="booking_contact_linked",
            event_channel="booking",
            event_direction=None,
            event_time=event_time,
            event_end_time=None,
            event_status="completed",
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

            if user_id not in (None, ""):
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="user_id",
                    key_value=str(user_id),
                    role="booking_contact",
                    direction_role=None,
                    raw_label=contact_name,
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                participants_written += 1 if participant_id else 0
                seq += 1

            if email:
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="email",
                    key_value=str(email),
                    role="booking_contact",
                    direction_role=None,
                    raw_label=contact_name,
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                participants_written += 1 if participant_id else 0
                seq += 1

            if mobile:
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="phone",
                    key_value=str(mobile),
                    role="booking_contact",
                    direction_role=None,
                    raw_label=contact_name,
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                participants_written += 1 if participant_id else 0
                seq += 1

            for context_type, context_value in self.source.extract_user_contact_info_contexts(r):
                context_id = self.context.add_context(
                    event_id=event_id,
                    context_type=context_type,
                    context_value=context_value,
                )
                contexts_written += 1 if context_id else 0

            if person_id:
                context_id = self.context.add_context(
                    event_id=event_id,
                    context_type="person",
                    context_value=str(person_id),
                )
                contexts_written += 1 if context_id else 0

        return {
            "processed": 1,
            "last_source_id": r.source_id,
            "created_events": created_events,
            "participants_written": participants_written,
            "contexts_written": contexts_written,
            "merged_people": merged_people,
            "repaired_participants": repaired_participants,
        }

    def run(self, batch_size=5000, limit=None, start_source_id=None):
        result = super().run(
            batch_size=batch_size,
            limit=limit,
            start_source_id=start_source_id,
        )
        return {
            "processed_user_contact_rows": result["processed_rows"],
            "created_events": result.get("created_events", 0),
            "participants_written": result.get("participants_written", 0),
            "contexts_written": result.get("contexts_written", 0),
            "merged_people": result.get("merged_people", 0),
            "repaired_participants": result.get("repaired_participants", 0),
            "last_source_id": result["last_source_id"],
        }

