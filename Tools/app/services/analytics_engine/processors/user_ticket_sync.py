from sqlalchemy import text

from ..processors.time_cursor_sync_processor import TimeCursorSyncProcessor
from ..core.service_container import ServiceContainer
from ..core.config import STAGING_TICKETS, STAGING_BOOKINGS
from ..processors.sync_support import safe_event_end_time


class UserTicketSync(TimeCursorSyncProcessor):
    PROCESSOR_NAME = "user_ticket_sync"
    SOURCE_TABLE = STAGING_TICKETS
    SOURCE_TABLE_NAME = "staging_user_ticket"

    def __init__(self, db):
        super().__init__(db, ServiceContainer(db), default_batch_size=3000)

    def fetch_rows_since(self, last_timestamp, last_source_id, batch_size: int):
        return self.source.fetch_user_tickets_since(
            last_timestamp=last_timestamp,
            last_source_id=last_source_id,
            batch_size=batch_size,
        )

    def fetch_rows_with_time_cursor(self, last_source_id, last_timestamp, batch_size: int):
        return self.source.fetch_user_tickets(
            last_source_id=last_source_id,
            last_timestamp=last_timestamp,
            batch_size=batch_size,
        )

    def get_row_cursor_timestamp(self, row, row_result=None):
        return getattr(row, "close_date", None) or getattr(row, "created_at", None) or getattr(row, "synced_at", None)

    def _lookup_booking_user(self, booking_id):
        if booking_id in (None, ""):
            return None

        try:
            booking_id = int(booking_id)
        except Exception:
            return None

        return self.db.execute(
            text(
                f"""
                SELECT booking_id, user_id, lead_id, prop_id
                FROM {STAGING_BOOKINGS}
                WHERE booking_id = :booking_id
                LIMIT 1
                """
            ),
            {"booking_id": booking_id},
        ).mappings().fetchone()

    def _base_meta(self, row, booking_row=None):
        meta = {
            "source_id": getattr(row, "source_id", None),
            "booking_id": getattr(row, "booking_id", None),
            "property_id": getattr(row, "prop_id", None),
            "building_id": getattr(row, "building_id", None),
            "building_name": getattr(row, "building_name", None),
            "category": getattr(row, "category", None),
            "priority": getattr(row, "priority", None),
            "description": getattr(row, "description", None),
            "mobile_number": getattr(row, "mobile_number", None),
            "unit_number": getattr(row, "unit_number", None),
            "status": getattr(row, "status", None),
            "reopen_flag": getattr(row, "reopen_flag", None),
            "assigned_to": getattr(row, "assigned_to", None),
            "resolved_by": getattr(row, "resolved_by", None),
            "closed_by": getattr(row, "closed_by", None),
            "active_days": getattr(row, "active_days", None),
            "labour_cost": getattr(row, "labour_cost", None),
            "material_cost": getattr(row, "material_cost", None),
            "total_cost": getattr(row, "total_cost", None),
            "ticket_rating": getattr(row, "ticket_rating", None),
            "ticket_feedback": getattr(row, "ticket_feedback", None),
            "booking_user_id": booking_row.get("user_id") if booking_row else None,
            "booking_lead_id": booking_row.get("lead_id") if booking_row else None,
            "booking_prop_id": booking_row.get("prop_id") if booking_row else None,
        }
        return {k: v for k, v in meta.items() if v is not None}

    def _create_ticket_event(self, row, event_name, event_time, event_end_time, metric_value, metric_name):
        booking_id = getattr(row, "booking_id", None)
        booking_row = self._lookup_booking_user(booking_id)

        event_meta = self._base_meta(row, booking_row=booking_row)
        event_id = self.events.create_event(
            event_family="support",
            event_name=event_name,
            event_channel="ticket",
            event_direction=None,
            event_time=event_time,
            event_end_time=event_end_time,
            event_status=getattr(row, "status", None),
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
            participant_data = self.source.extract_user_ticket_participants(row)

            booking_user_id = booking_row.get("user_id") if booking_row else None
            if booking_user_id not in (None, ""):
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
                if participant_id:
                    seq += 1

            mobile_number = participant_data.get("mobile_number")
            if mobile_number:
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="phone",
                    key_value=str(mobile_number),
                    role="ticket_requester",
                    direction_role="from",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=row.source_id,
                    event_time=event_time,
                )
                participants_written += 1 if participant_id else 0
                if participant_id:
                    seq += 1

            assigned_to = participant_data.get("assigned_to")
            if assigned_to and str(assigned_to).strip().lower() not in {"unassigned", "0", ""}:
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="staff_ref",
                    key_value=str(assigned_to).strip(),
                    role="assigned_to",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=row.source_id,
                    event_time=event_time,
                )
                participants_written += 1 if participant_id else 0
                if participant_id:
                    seq += 1

            resolved_by = participant_data.get("resolved_by")
            if resolved_by and str(resolved_by).strip():
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="staff_ref",
                    key_value=str(resolved_by).strip(),
                    role="resolved_by",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=row.source_id,
                    event_time=event_time,
                )
                participants_written += 1 if participant_id else 0
                if participant_id:
                    seq += 1

            closed_by = participant_data.get("closed_by")
            if closed_by and str(closed_by).strip():
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="staff_ref",
                    key_value=str(closed_by).strip(),
                    role="closed_by",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=row.source_id,
                    event_time=event_time,
                )
                participants_written += 1 if participant_id else 0
                if participant_id:
                    seq += 1

            if booking_id:
                cid = self.context.add_context(event_id, "booking", str(booking_id))
                contexts_written += 1 if cid else 0

            prop_id = getattr(row, "prop_id", None) or (booking_row.get("prop_id") if booking_row else None)
            if prop_id:
                cid = self.context.add_context(event_id, "property", str(prop_id))
                contexts_written += 1 if cid else 0

            building_id = getattr(row, "building_id", None)
            if building_id:
                cid = self.context.add_context(event_id, "building", str(building_id))
                contexts_written += 1 if cid else 0

        return event_id, participants_written, contexts_written

    def process_row(self, r):
        created_events = 0
        participants_written = 0
        contexts_written = 0

        created_time = self.source.extract_ticket_created_time(r)
        close_time = self.source.extract_ticket_resolved_time(r)

        if created_time:
            event_id, p_written, c_written = self._create_ticket_event(
                r,
                event_name="ticket_created",
                event_time=created_time,
                event_end_time=safe_event_end_time(created_time, close_time),
                metric_value=None,
                metric_name=None,
            )
            created_events += 1 if event_id else 0
            participants_written += p_written
            contexts_written += c_written

        if close_time:
            metric_value = getattr(r, "ticket_rating", None)
            event_id, p_written, c_written = self._create_ticket_event(
                r,
                event_name="ticket_resolved",
                event_time=close_time,
                event_end_time=None,
                metric_value=metric_value,
                metric_name="ticket_rating" if metric_value is not None else None,
            )
            created_events += 1 if event_id else 0
            participants_written += p_written
            contexts_written += c_written

        return {
            "processed": 1,
            "last_source_id": r.source_id,
            "last_timestamp": close_time or created_time or getattr(r, "synced_at", None),
            "created_events": created_events,
            "participants_written": participants_written,
            "contexts_written": contexts_written,
        }

    def run(self, batch_size=3000, limit=None, start_source_id=None):
        result = super().run(batch_size=batch_size, limit=limit, start_source_id=start_source_id)
        return {
            "processed_user_ticket_rows": result["processed_rows"],
            "created_events": result.get("created_events", 0),
            "participants_written": result.get("participants_written", 0),
            "contexts_written": result.get("contexts_written", 0),
            "last_source_id": result["last_source_id"],
        }
