from __future__ import annotations

from ..core.config import STAGING_CALL_LOG
from ..core.service_container import ServiceContainer
from ..core.utils import compute_event_end_time, normalize_call_status, normalize_event_direction
from ..processors.base_sync_processor import BaseSyncProcessor


class CallLogSync(BaseSyncProcessor):
    PROCESSOR_NAME = "call_log_sync"
    SOURCE_TABLE = STAGING_CALL_LOG
    SOURCE_TABLE_NAME = "staging_call_log_unified"

    def __init__(self, db):
        super().__init__(db, ServiceContainer(db), default_batch_size=5000)

    def reset_batch_state(self):
        # Override BaseSyncProcessor behavior for call logs.
        # Keeping identity cache across batches avoids re-resolving the same
        # phones / staff refs again and again in later batches.
        return None

    def fetch_rows(self, last_source_id: int, batch_size: int):
        """
        Fetch call rows from AnalyticsEngine.staging_call_log_unified.

        Important:
        - This processor must use a time cursor because unified rows can be
          updated later when transcript/audio/matching data arrives.
        - BaseSyncProcessor passes last_source_id, but not last_timestamp.
          So we read last_timestamp from processor_checkpoint here.
        """
        checkpoint = self.checkpoints.get_checkpoint(self.processor_name) or {}
        last_timestamp = checkpoint.get("last_timestamp")

        return self.source.fetch_call_logs(
            last_source_id=last_source_id,
            batch_size=batch_size,
            last_timestamp=last_timestamp,
        )

    def get_row_checkpoint_timestamp(self, row, row_result=None):
        """
        Store the unified-row update cursor in processor_checkpoint.

        Prefer updated_at because staging_call_log_unified changes when an
        existing call row is enriched with transcript/audio/match data.
        """
        return getattr(row, "updated_at", None) or getattr(row, "synced_at", None)

    def process_row(self, r):
        event_direction = normalize_event_direction(r.call_direction, r.call_result)
        event_status = normalize_call_status(r.call_result)
        event_end_time = compute_event_end_time(r.call_time, r.talk_time_sec)

        transcript = (
            getattr(r, "translated_text", None)
            or getattr(r, "transcript_text", None)
            or getattr(r, "transcript_text_eleven_labs", None)
            or getattr(r, "raw_transcripts", None)
        )

        meta = {
            "raw_call_direction": getattr(r, "call_direction", None),
            "raw_call_result": getattr(r, "call_result", None),
            "raw_talk_time_sec": getattr(r, "talk_time_sec", None),
            "counterparty_phone": getattr(r, "counterparty_phone", None),
            "sales_phone": getattr(r, "sales_phone", None),
            "executive_id": getattr(r, "executive_id", None),
            "executive_name": getattr(r, "executive_name", None),
            "lead_id": getattr(r, "lead_id", None),
            "rms_source_id": getattr(r, "rms_source_id", None),
            "recording_source_id": getattr(r, "recording_source_id", None),
            "match_status": getattr(r, "match_status", None),
            "match_confidence": getattr(r, "match_confidence", None),
            "match_reason": getattr(r, "match_reason", None),
            "department": getattr(r, "department", None),
            "audio_url": getattr(r, "audio_url", None),
            "transcript_text": transcript,
            "transcript_source": (
                "translated_text" if getattr(r, "translated_text", None)
                else "transcript_text" if getattr(r, "transcript_text", None)
                else "transcript_text_eleven_labs" if getattr(r, "transcript_text_eleven_labs", None)
                else "raw_transcripts" if getattr(r, "raw_transcripts", None)
                else None
            ),
            "action_layer": getattr(r, "action_layer", None),
            "context": getattr(r, "context", None),
            "language": getattr(r, "language", None),
            "source_call_id": getattr(r, "source_call_id", None),
            "filename": getattr(r, "filename", None),
            "uploaded_at": getattr(r, "uploaded_at", None),
            "source_status": getattr(r, "source_status", None),
            "sync_status": getattr(r, "sync_status", None),
            "updated_at": getattr(r, "updated_at", None),
            "synced_at": getattr(r, "synced_at", None),
        }
        meta = {k: v for k, v in meta.items() if v not in (None, "", [], {})}

        event_id = self.events.create_event(
            event_family="communication",
            event_name="call",
            event_channel="call",
            event_direction=event_direction,
            event_time=r.call_time,
            event_end_time=event_end_time,
            event_status=event_status,
            source_table=STAGING_CALL_LOG,
            source_id=str(r.source_id),
            metric_value=r.talk_time_sec,
            metric_unit="seconds",
            metric_name="duration",
            meta=meta,
        )

        participants_written = 0
        contexts_written = 0
        created_events = 1 if event_id else 0

        if event_id:
            phone1, phone2 = self.source.extract_call_participants(r)
            seq = 1

            if phone1:
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="phone",
                    key_value=phone1,
                    role="counterparty",
                    direction_role="from",
                    source_table=STAGING_CALL_LOG,
                    source_id=r.source_id,
                    event_time=r.call_time,
                )
                participants_written += 1 if participant_id else 0
                seq += 1

            if phone2:
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="phone",
                    key_value=phone2,
                    role="sales_line",
                    direction_role="to",
                    source_table=STAGING_CALL_LOG,
                    source_id=r.source_id,
                    event_time=r.call_time,
                )
                participants_written += 1 if participant_id else 0
                seq += 1

            executive_id = getattr(r, "executive_id", None)
            if executive_id:
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="staff_ref",
                    key_value=str(executive_id).strip(),
                    role="executive",
                    source_table=STAGING_CALL_LOG,
                    source_id=r.source_id,
                    event_time=r.call_time,
                )
                participants_written += 1 if participant_id else 0

            lead_id = getattr(r, "lead_id", None)
            if lead_id:
                context_id = self.context.add_context(
                    event_id=event_id,
                    context_type="lead",
                    context_value=str(lead_id),
                )
                contexts_written += 1 if context_id else 0

        return {
            "processed": 1,
            "last_source_id": r.source_id,
            "created_events": created_events,
            "participants_written": participants_written,
            "contexts_written": contexts_written,
        }

    def run(self, batch_size=5000, limit=None, start_source_id=None):
        result = super().run(
            batch_size=batch_size,
            limit=limit,
            start_source_id=start_source_id,
        )
        return {
            "processed_call_rows": result["processed_rows"],
            "created_events": result.get("created_events", 0),
            "participants_written": result.get("participants_written", 0),
            "contexts_written": result.get("contexts_written", 0),
            "ontology_events_tagged": result.get("ontology_events_tagged", 0),
            "ontology_tags_written": result.get("ontology_tags_written", 0),
            "state_rows_written": result.get("state_rows_written", 0),
            "last_source_id": result["last_source_id"],
        }