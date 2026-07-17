from ..processors.base_sync_processor import BaseSyncProcessor
from ..core.service_container import ServiceContainer
from ..core.config import STAGING_LEADS


class LeadSync(BaseSyncProcessor):
    PROCESSOR_NAME = "lead_sync"
    SOURCE_TABLE = STAGING_LEADS
    SOURCE_TABLE_NAME = "staging_lead_tracking"

    def __init__(self, db):
        super().__init__(db, ServiceContainer(db), default_batch_size=5000)

    def fetch_rows(self, last_source_id: int, batch_size: int):
        return self.source.fetch_leads(
            last_source_id=last_source_id,
            batch_size=batch_size,
        )

    def get_row_checkpoint_timestamp(self, row, row_result=None):
        return self.source.extract_lead_event_time(row)


    def process_row(self, r):
        created_at = getattr(r, "created_at", None)
        closed_at = getattr(r, "closed_at", None)
        event_time = created_at or closed_at or getattr(r, "resolved_at", None) or getattr(r, "synced_at", None)

        if not created_at and not closed_at:
            return {
                "processed": 1,
                "last_source_id": r.source_id,
            }

        merged_people = 0
        repaired_participants = 0

        candidate_keys = self.source.build_lead_customer_candidate_keys(r)
        seed_fields = self.source.build_lead_customer_seed_fields(r)

        if candidate_keys:
            result = self.identity.resolve_or_create_person_from_keys(
                candidate_keys=candidate_keys,
                source_table=self.SOURCE_TABLE_NAME,
                source_id=str(r.source_id),
                event_time=event_time,
                seed_fields=seed_fields,
                merge_reason="lead_customer_bridge",
                return_details=True,
            )

            person_id = result["person_id"] if isinstance(result, dict) else result
            merged_people = int(result.get("merged_people", 0)) if isinstance(result, dict) else 0

            if person_id:
                repaired_participants = self.identity.reassign_event_participants_for_keys(
                    person_id=person_id,
                    candidate_keys=candidate_keys,
                )
                try:
                    setattr(r, "person_id", person_id)
                except Exception:
                    pass

        event_meta = self.source.build_lead_event_meta(r)

        participants_written = 0
        contexts_written = 0
        lead_facts_written = 0
        created_events = 0

        lead_fact_payload = self.source.build_lead_fact_payload(r)

        resolved_person_id = getattr(r, "person_id", None) or lead_fact_payload.get("person_id")
        if resolved_person_id:
            lead_fact_payload["person_id"] = resolved_person_id

        def _normalized_text(value):
            if value is None:
                return None
            value = str(value).strip()
            return value or None

        def _write_customer_participant(event_id, event_time, seq):
            written = 0

            user_id = getattr(r, "user_id", None)
            primary_phone, alt_phone, email, _executive_ref = self.source.extract_lead_participants(r)

            if user_id not in (None, ""):
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="user_id",
                    key_value=str(user_id).strip(),
                    role="lead_owner",
                    direction_role="from",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                written += 1 if participant_id else 0
                seq += 1
                return seq, written

            if primary_phone:
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="phone",
                    key_value=primary_phone,
                    role="lead_owner_phone",
                    direction_role="from",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                written += 1 if participant_id else 0
                seq += 1
                return seq, written

            if email:
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="email",
                    key_value=email,
                    role="lead_owner_email",
                    direction_role="from",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                written += 1 if participant_id else 0
                seq += 1
                return seq, written

            if alt_phone:
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="phone",
                    key_value=alt_phone,
                    role="lead_owner_phone_alt",
                    direction_role="from",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                written += 1 if participant_id else 0
                seq += 1

            return seq, written

        def _write_staff_participants(event_id, event_time, seq, include_origin_roles):
            written = 0

            _primary_phone, _alt_phone, _email, executive_ref = self.source.extract_lead_participants(r)

            staff_roles = [
                ("executive", executive_ref),
                ("assigned_to", getattr(r, "assigned_to", None)),
            ]

            if include_origin_roles:
                staff_roles.extend(
                    [
                        ("added_by", getattr(r, "added_by", None)),
                        ("generated_by", getattr(r, "generated_by", None)),
                    ]
                )

            seen = set()
            for role, staff_ref in staff_roles:
                value = _normalized_text(staff_ref)
                if not value:
                    continue

                dedupe_key = (role, value.lower())
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)

                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="staff_ref",
                    key_value=value,
                    role=role,
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                written += 1 if participant_id else 0
                seq += 1

            return seq, written

        def _write_contexts(event_id):
            written = 0
            for context_type, context_value in self.source.extract_lead_contexts(r):
                context_id = self.context.add_context(
                    event_id=event_id,
                    context_type=context_type,
                    context_value=context_value,
                )
                written += 1 if context_id else 0
            return written

        def _create_lead_event(*, event_name, event_time, source_id_value):
            event_status = getattr(r, "raw_status", None)
            if event_status is not None:
                event_status = str(event_status).strip() or None

            return self.events.create_event(
                event_family="lead",
                event_name=event_name,
                event_channel="lead",
                event_direction="inbound",
                event_time=event_time,
                event_end_time=None,
                event_status=event_status,
                source_table=self.SOURCE_TABLE_NAME,
                source_id=str(source_id_value),
                metric_value=None,
                metric_unit=None,
                metric_name=None,
                meta=event_meta,
            )

        if created_at:
            created_event_id = _create_lead_event(
                event_name="lead_created",
                event_time=created_at,
                source_id_value=r.source_id,
            )

            if created_event_id:
                created_events += 1

                seq = 1
                seq, added = _write_customer_participant(created_event_id, created_at, seq)
                participants_written += added

                seq, added = _write_staff_participants(
                    created_event_id,
                    created_at,
                    seq,
                    include_origin_roles=True,
                )
                participants_written += added

                contexts_written += _write_contexts(created_event_id)

                lead_fact_id = self.lead_facts.upsert_lead_fact(
                    event_id=created_event_id,
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=str(r.source_id),
                    lead_id=lead_fact_payload.get("lead_id"),
                    booking_id=lead_fact_payload.get("booking_id"),
                    user_id=lead_fact_payload.get("user_id"),
                    person_id=lead_fact_payload.get("person_id"),
                    actor_id=lead_fact_payload.get("actor_id"),
                    executive_ref=lead_fact_payload.get("executive_ref"),
                    assigned_to=lead_fact_payload.get("assigned_to"),
                    added_by=lead_fact_payload.get("added_by"),
                    generated_by=lead_fact_payload.get("generated_by"),
                    origin=lead_fact_payload.get("origin"),
                    raw_status=lead_fact_payload.get("raw_status"),
                    is_resolved=lead_fact_payload.get("is_resolved"),
                    match_type=lead_fact_payload.get("match_type"),
                    resolved_at=lead_fact_payload.get("resolved_at"),
                    created_at_source=lead_fact_payload.get("created_at_source"),
                    closed_at_source=lead_fact_payload.get("closed_at_source"),
                    synced_at_source=lead_fact_payload.get("synced_at_source"),
                    priority=lead_fact_payload.get("priority"),
                    contact_number=lead_fact_payload.get("contact_number"),
                    contact_number_alt=lead_fact_payload.get("contact_number_alt"),
                    email=lead_fact_payload.get("email"),
                    raw_payload=lead_fact_payload.get("raw_payload"),
                )
                lead_facts_written += 1 if lead_fact_id else 0

        if closed_at:
            closed_event_id = _create_lead_event(
                event_name="lead_closed",
                event_time=closed_at,
                source_id_value=f"{r.source_id}:closed",
            )

            if closed_event_id:
                created_events += 1

                seq = 1
                seq, added = _write_customer_participant(closed_event_id, closed_at, seq)
                participants_written += added

                seq, added = _write_staff_participants(
                    closed_event_id,
                    closed_at,
                    seq,
                    include_origin_roles=False,
                )
                participants_written += added

                contexts_written += _write_contexts(closed_event_id)

        return {
            "processed": 1,
            "last_source_id": r.source_id,
            "created_events": created_events,
            "lead_facts_written": lead_facts_written,
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
            "processed_lead_rows": result["processed_rows"],
            "created_events": result.get("created_events", 0),
            "lead_facts_written": result.get("lead_facts_written", 0),
            "participants_written": result.get("participants_written", 0),
            "contexts_written": result.get("contexts_written", 0),
            "merged_people": result.get("merged_people", 0),
            "repaired_participants": result.get("repaired_participants", 0),
            "last_source_id": result["last_source_id"],
        }
