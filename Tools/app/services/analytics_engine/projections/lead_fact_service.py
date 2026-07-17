import json

from sqlalchemy import text

from ..core.config import LEAD_FACT


class LeadFactService:
    def __init__(self, db):
        self.db = db
        self._sql_upsert = text(f"""
            INSERT INTO {LEAD_FACT}
            (
                event_id,
                source_table,
                source_id,
                lead_id,
                booking_id,
                user_id,
                person_id,
                actor_id,
                executive_ref,
                assigned_to,
                added_by,
                generated_by,
                origin,
                raw_status,
                is_resolved,
                match_type,
                resolved_at,
                created_at_source,
                closed_at_source,
                synced_at_source,
                priority,
                contact_number,
                contact_number_alt,
                email,
                raw_payload
            )
            VALUES
            (
                :event_id,
                :source_table,
                :source_id,
                :lead_id,
                :booking_id,
                :user_id,
                :person_id,
                :actor_id,
                :executive_ref,
                :assigned_to,
                :added_by,
                :generated_by,
                :origin,
                :raw_status,
                :is_resolved,
                :match_type,
                :resolved_at,
                :created_at_source,
                :closed_at_source,
                :synced_at_source,
                :priority,
                :contact_number,
                :contact_number_alt,
                :email,
                CAST(:raw_payload AS JSONB)
            )
            ON CONFLICT (source_table, source_id)
            DO UPDATE SET
                event_id = EXCLUDED.event_id,
                lead_id = COALESCE(EXCLUDED.lead_id, {LEAD_FACT}.lead_id),
                booking_id = COALESCE(EXCLUDED.booking_id, {LEAD_FACT}.booking_id),
                user_id = COALESCE(EXCLUDED.user_id, {LEAD_FACT}.user_id),
                person_id = COALESCE(EXCLUDED.person_id, {LEAD_FACT}.person_id),
                actor_id = COALESCE(EXCLUDED.actor_id, {LEAD_FACT}.actor_id),
                executive_ref = COALESCE(EXCLUDED.executive_ref, {LEAD_FACT}.executive_ref),
                assigned_to = COALESCE(EXCLUDED.assigned_to, {LEAD_FACT}.assigned_to),
                added_by = COALESCE(EXCLUDED.added_by, {LEAD_FACT}.added_by),
                generated_by = COALESCE(EXCLUDED.generated_by, {LEAD_FACT}.generated_by),
                origin = COALESCE(EXCLUDED.origin, {LEAD_FACT}.origin),
                raw_status = COALESCE(EXCLUDED.raw_status, {LEAD_FACT}.raw_status),
                is_resolved = COALESCE(EXCLUDED.is_resolved, {LEAD_FACT}.is_resolved),
                match_type = COALESCE(EXCLUDED.match_type, {LEAD_FACT}.match_type),
                resolved_at = COALESCE(EXCLUDED.resolved_at, {LEAD_FACT}.resolved_at),
                created_at_source = COALESCE(EXCLUDED.created_at_source, {LEAD_FACT}.created_at_source),
                closed_at_source = COALESCE(EXCLUDED.closed_at_source, {LEAD_FACT}.closed_at_source),
                synced_at_source = COALESCE(EXCLUDED.synced_at_source, {LEAD_FACT}.synced_at_source),
                priority = COALESCE(EXCLUDED.priority, {LEAD_FACT}.priority),
                contact_number = COALESCE(EXCLUDED.contact_number, {LEAD_FACT}.contact_number),
                contact_number_alt = COALESCE(EXCLUDED.contact_number_alt, {LEAD_FACT}.contact_number_alt),
                email = COALESCE(EXCLUDED.email, {LEAD_FACT}.email),
                raw_payload = COALESCE(EXCLUDED.raw_payload, {LEAD_FACT}.raw_payload),
                updated_at = NOW()
            RETURNING lead_fact_id
        """)

    def upsert_lead_fact(
        self,
        event_id,
        source_table,
        source_id,
        lead_id=None,
        booking_id=None,
        user_id=None,
        person_id=None,
        actor_id=None,
        executive_ref=None,
        assigned_to=None,
        added_by=None,
        generated_by=None,
        origin=None,
        raw_status=None,
        is_resolved=None,
        match_type=None,
        resolved_at=None,
        created_at_source=None,
        closed_at_source=None,
        synced_at_source=None,
        priority=None,
        contact_number=None,
        contact_number_alt=None,
        email=None,
        raw_payload=None,
    ):
        row = self.db.execute(
            self._sql_upsert,
            {
                "event_id": event_id,
                "source_table": source_table,
                "source_id": str(source_id),
                "lead_id": str(lead_id) if lead_id is not None else None,
                "booking_id": str(booking_id) if booking_id is not None else None,
                "user_id": str(user_id) if user_id is not None else None,
                "person_id": str(person_id) if person_id is not None else None,
                "actor_id": str(actor_id) if actor_id is not None else None,
                "executive_ref": str(executive_ref).strip() if executive_ref is not None else None,
                "assigned_to": str(assigned_to).strip() if assigned_to is not None else None,
                "added_by": str(added_by).strip() if added_by is not None else None,
                "generated_by": str(generated_by).strip() if generated_by is not None else None,
                "origin": origin,
                "raw_status": raw_status,
                "is_resolved": is_resolved,
                "match_type": match_type,
                "resolved_at": resolved_at,
                "created_at_source": created_at_source,
                "closed_at_source": closed_at_source,
                "synced_at_source": synced_at_source,
                "priority": priority,
                "contact_number": contact_number,
                "contact_number_alt": contact_number_alt,
                "email": email,
                "raw_payload": json.dumps(raw_payload or {}, default=str),
            },
        ).fetchone()
        return row.lead_fact_id if row else None
