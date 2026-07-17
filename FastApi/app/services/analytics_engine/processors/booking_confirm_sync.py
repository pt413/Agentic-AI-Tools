from ..processors.base_sync_processor import BaseSyncProcessor
from ..core.service_container import ServiceContainer
from ..core.config import STAGING_BOOKINGS
from ..processors.sync_support import add_staff_participant

class BookingConfirmSync(BaseSyncProcessor):
    PROCESSOR_NAME = "booking_confirm_sync"
    SOURCE_TABLE = STAGING_BOOKINGS
    SOURCE_TABLE_NAME = "staging_booking_confirm"

    def __init__(self, db):
        super().__init__(db, ServiceContainer(db), default_batch_size=5000)

    def fetch_rows(self, last_source_id: int, batch_size: int):
        return self.source.fetch_booking_confirms(
            last_source_id=last_source_id,
            batch_size=batch_size,
        )

    def process_row(self, r):
        booking_time = self.source.extract_booking_event_time(r)
        if not booking_time:
            return {
                "processed": 1,
                "last_source_id": r.source_id,
            }

        metric_value = self.source.extract_booking_metric_value(r)
        metric_name = self.source.extract_booking_metric_name(r)
        event_meta = self.source.build_booking_event_meta(r)

        customer_phone, sales_phone, executive_id = self.source.extract_booking_participants(r)

        user_id = getattr(r, "user_id", None)
        created_by_raw = self.source.normalize_actor_ref(getattr(r, "created_by", None))

        candidate_keys = []

        if user_id not in (None, ""):
            candidate_keys.append(("user_id", str(user_id)))

        if customer_phone:
            candidate_keys.append(("phone", str(customer_phone)))

        seed_fields = {
            "primary_phone": customer_phone,
            "primary_email": None,
            "canonical_name": None,
            "person_kind": "external" if candidate_keys else "unknown",
            "kind_confidence": 1 if candidate_keys else 0,
        }

        person_id = None
        merged_people = 0
        repaired_participants = 0

        if candidate_keys:
            result = self.identity.resolve_or_create_person_from_keys(
                candidate_keys=candidate_keys,
                source_table=self.SOURCE_TABLE_NAME,
                source_id=str(r.source_id),
                event_time=booking_time,
                seed_fields=seed_fields,
                merge_reason="booking_confirm_bridge",
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

        if created_by_raw:
            if self.source.is_system_actor(created_by_raw):
                event_meta["created_by_mode"] = "system"
                event_meta["created_by_raw"] = created_by_raw
            else:
                event_meta["created_by_mode"] = "actor"
                event_meta["created_by_raw"] = created_by_raw

        event_id = self.events.create_event(
            event_family="booking",
            event_name="booking_confirm",
            event_channel="booking",
            event_direction=None,
            event_time=booking_time,
            event_end_time=None,
            event_status=self.source.extract_booking_status(r),
            source_table=self.SOURCE_TABLE_NAME,
            source_id=str(r.source_id),
            metric_value=metric_value,
            metric_unit=None,
            metric_name=metric_name,
            meta=event_meta,
        )

        participants_written = 0
        contexts_written = 0
        booking_facts_written = 0
        created_events = 1 if event_id else 0

        if event_id:
            booking_fact_payload = self.source.build_booking_fact_payload(r)
            booking_fact_id = self.booking_facts.upsert_booking_fact(
                event_id=event_id,
                source_table=self.SOURCE_TABLE_NAME,
                source_id=str(r.source_id),
                booking_id=booking_fact_payload.get("booking_id"),
                lead_id=booking_fact_payload.get("lead_id"),
                property_id=booking_fact_payload.get("property_id"),
                customer_phone=booking_fact_payload.get("customer_phone"),
                sales_phone=booking_fact_payload.get("sales_phone"),
                executive_ref=booking_fact_payload.get("executive_ref"),
                booking_status=booking_fact_payload.get("booking_status"),
                booking_amount=booking_fact_payload.get("booking_amount"),
                currency_code=booking_fact_payload.get("currency_code"),
                booking_time=booking_fact_payload.get("booking_time"),
                raw_payload=booking_fact_payload.get("raw_payload"),
            )
            booking_facts_written += 1 if booking_fact_id else 0

            seq = 1

            if user_id not in (None, ""):
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="user_id",
                    key_value=str(user_id),
                    role="customer",
                    direction_role="from",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=booking_time,
                )
                participants_written += 1 if participant_id else 0
                seq += 1
            elif customer_phone:
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="phone",
                    key_value=customer_phone,
                    role="customer",
                    direction_role="from",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=booking_time,
                )
                participants_written += 1 if participant_id else 0
                seq += 1

            if sales_phone:
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="phone",
                    key_value=sales_phone,
                    role="sales_line",
                    direction_role="to",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=booking_time,
                )
                participants_written += 1 if participant_id else 0
                seq += 1

            seq, added = add_staff_participant(
                self,
                event_id=event_id,
                participant_seq=seq,
                staff_ref=executive_id,
                role="executive",
                source_table=self.SOURCE_TABLE_NAME,
                source_id=r.source_id,
                event_time=booking_time,
            )
            participants_written += added

            for context_type, context_value in self.source.extract_booking_contexts(r):
                context_id = self.context.add_context(
                    event_id=event_id,
                    context_type=context_type,
                    context_value=context_value,
                )
                contexts_written += 1 if context_id else 0

        return {
            "processed": 1,
            "last_source_id": r.source_id,
            "created_events": created_events,
            "booking_facts_written": booking_facts_written,
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
            "processed_booking_rows": result["processed_rows"],
            "created_events": result.get("created_events", 0),
            "booking_facts_written": result.get("booking_facts_written", 0),
            "participants_written": result.get("participants_written", 0),
            "contexts_written": result.get("contexts_written", 0),
            "merged_people": result.get("merged_people", 0),
            "repaired_participants": result.get("repaired_participants", 0),
            "last_source_id": result["last_source_id"],
        }
