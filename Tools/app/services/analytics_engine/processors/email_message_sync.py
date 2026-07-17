from ..processors.base_sync_processor import BaseSyncProcessor
from ..core.service_container import ServiceContainer
from ..core.config import STAGING_EMAILS
from ..processors.sync_support import add_email_string_participants, append_contexts, normalize_message_direction


class EmailMessageSync(BaseSyncProcessor):
    PROCESSOR_NAME = "email_message_sync"
    SOURCE_TABLE = STAGING_EMAILS
    SOURCE_TABLE_NAME = "staging_email_messages"

    def __init__(self, db):
        super().__init__(db, ServiceContainer(db), default_batch_size=3000)

    def fetch_rows(self, last_source_id: int, batch_size: int):
        return self.source.fetch_email_messages(last_source_id=last_source_id, batch_size=batch_size)

    def process_row(self, r):
        event_time = getattr(r, "email_date", None)
        if not event_time:
            return {
                "processed": 1,
                "last_source_id": r.source_id,
            }

        raw_direction = (getattr(r, "direction", None) or "").strip().lower()
        if raw_direction in {"outgoing", "outbound", "sent"}:
            event_direction = "outbound"
            event_name = "email_outbound"
        elif raw_direction in {"incoming", "inbound", "received"}:
            event_direction = "inbound"
            event_name = "email_inbound"
        else:
            event_direction = None
            event_name = "email_message"

        body = getattr(r, "body", None) or ""
        snippet = getattr(r, "snippet", None) or ""
        content_length = len(body) if body else len(snippet) if snippet else None

        event_meta = {
            "source_id": getattr(r, "source_id", None),
            "msgid": getattr(r, "msgid", None),
            "subject": getattr(r, "subject", None),
            "direction": getattr(r, "direction", None),
            "sender": getattr(r, "sender", None),
            "receiver": getattr(r, "receiver", None),
            "thread_id": getattr(r, "thread_id", None),
            "snippet": snippet if snippet else None,
        }
        event_meta = {k: v for k, v in event_meta.items() if v is not None}

        event_id = self.events.create_event(
            event_family="communication",
            event_name=event_name,
            event_channel="email",
            event_direction=event_direction,
            event_time=event_time,
            event_end_time=None,
            event_status=None,
            source_table=self.SOURCE_TABLE_NAME,
            source_id=str(r.source_id),
            metric_value=content_length,
            metric_unit=None,
            metric_name="content_length",
            meta=event_meta,
        )

        participants_written = 0
        contexts_written = 0
        created_events = 1 if event_id else 0

        if event_id:
            seq = 1

            sender = getattr(r, "sender", None)
            if sender and str(sender).strip():
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=seq,
                    key_type="email",
                    key_value=str(sender).strip(),
                    role="sender",
                    direction_role="from",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                participants_written += 1 if participant_id else 0
                seq += 1

            receiver = getattr(r, "receiver", None)
            if receiver and str(receiver).strip():
                raw_receivers = str(receiver)
                receiver_list = [x.strip() for x in raw_receivers.replace(";", ",").split(",") if x.strip()]

                for rcpt in receiver_list[:10]:
                    participant_id = self.participants.add_participant(
                        event_id=event_id,
                        participant_seq=seq,
                        key_type="email",
                        key_value=rcpt,
                        role="receiver",
                        direction_role="to",
                        source_table=self.SOURCE_TABLE_NAME,
                        source_id=r.source_id,
                        event_time=event_time,
                    )
                    participants_written += 1 if participant_id else 0
                    seq += 1

            thread_id = getattr(r, "thread_id", None)
            if thread_id and str(thread_id).strip():
                cid = self.context.add_context(
                    event_id=event_id,
                    context_type="email_thread",
                    context_value=str(thread_id).strip(),
                )
                contexts_written += 1 if cid else 0

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
            "processed_email_rows": result["processed_rows"],
            "created_events": result.get("created_events", 0),
            "participants_written": result.get("participants_written", 0),
            "contexts_written": result.get("contexts_written", 0),
            "last_source_id": result["last_source_id"],
        }

