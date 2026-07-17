import re

from ..core.service_container import ServiceContainer
from ..core.config import STAGING_WHATSAPP
from ..core.utils import (
    build_whatsapp_message_preview,
    detect_whatsapp_message_ontology,
)
from ..processors.sync_support import (
    add_phone_participant,
    add_staff_participant,
    append_contexts,
    normalize_message_direction,
)
from ..processors.time_cursor_sync_processor import TimeCursorSyncProcessor


PROPERTY_SHARE_MARKERS = (
    "no brokerage",
    "building name",
    "caretaker number",
    "click for photos",
    "click for booking",
    "fully furnished",
    "semi furnished",
    "unfurnished",
    "rent",
    "deposit",
    "1bhk",
    "2bhk",
    "3bhk",
    "4bhk",
    "studio",
)

PROPERTY_SHARE_REFUSAL_PHRASES = (
    "not available",
    "no availability",
    "already booked",
    "already occupied",
    "fully occupied",
    "no vacancy",
    "unavailable",
    "sold out",
    "cannot provide",
    "can't provide",
    "unable to provide",
    "unable to share",
    "cannot share",
)

PAYMENT_REFERENCE_MARKERS = (
    "utr",
    "upi",
    "transaction id",
    "txn id",
    "payment received",
    "paid via",
    "paid using",
    "receipt",
    "invoice",
)


def _normalize_ontology_text(value):
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def _looks_like_property_share_message(clean_content, message_type, direction):
    text = _normalize_ontology_text(clean_content)
    if not text:
        return False

    msg_type = _normalize_ontology_text(message_type)
    msg_direction = _normalize_ontology_text(direction)

    if msg_type not in {"", "text", "template", "extendedtextmessage"}:
        return False

    if msg_direction and msg_direction not in {"outbound", "outgoing", "sent", "reply", "from_admin"}:
        return False

    if any(phrase in text for phrase in PROPERTY_SHARE_REFUSAL_PHRASES):
        return False

    marker_hits = sum(1 for marker in PROPERTY_SHARE_MARKERS if marker in text)

    has_bhk = bool(re.search(r"\b[1-9]\s*bhk\b", text))
    has_rent = "rent" in text
    has_deposit = "deposit" in text
    has_caretaker = "caretaker number" in text
    has_building = "building name" in text
    has_listing_link = ("click for photos" in text) or ("click for booking" in text)

    if marker_hits >= 3:
        return True

    if has_building and (has_rent or has_deposit):
        return True

    if has_bhk and (has_rent or has_deposit) and (has_caretaker or has_listing_link):
        return True

    return False


def _apply_property_share_guard(ontology, *, clean_content, message_type, direction):
    if not _looks_like_property_share_message(clean_content, message_type, direction):
        return ontology

    text = _normalize_ontology_text(clean_content)
    msg_type = _normalize_ontology_text(message_type)

    guarded = dict(ontology or {})
    guarded.update(
        {
            "message_kind": "template" if msg_type == "template" else "text",
            "speech_act": "share_information",
            "topic_primary": "location_property",
            "intent_primary": "share_location",
            "journey_stage": "pre_commitment",
            "resolution_stage": "informational",
            "urgency": "medium",
            "requires_reply": True,
            "is_actionable": True,
            "contains_amount": ("rent" in text or "deposit" in text or bool(re.search(r"\b\d{4,}\b", text))),
            "contains_payment_reference": any(marker in text for marker in PAYMENT_REFERENCE_MARKERS),
            "contains_location_reference": (
                "building name" in text
                or "location" in text
                or bool(re.search(r"\b(?:1|2|3|4)\s*bhk\b", text))
            ),
            "tagged_from": "rule_engine_v2_message_property_guard",
            "confidence": {
                "channel": 0.99,
                "direction": 0.9,
                "message_kind": 0.95,
                "speech_act": 0.96,
                "topic_primary": 0.96,
                "intent_primary": 0.95,
                "journey_stage": 0.82,
                "resolution_stage": 0.9,
                "urgency": 0.72,
            },
        }
    )
    return guarded


class WhatsAppMessageSync(TimeCursorSyncProcessor):
    PROCESSOR_NAME = "whatsapp_message_sync"
    SOURCE_TABLE = STAGING_WHATSAPP
    SOURCE_TABLE_NAME = "staging_whatsapp_messages"

    def __init__(self, db):
        super().__init__(db, ServiceContainer(db), default_batch_size=3000)

    def fetch_rows_since(self, last_timestamp, last_source_id, batch_size: int):
        return self.source.fetch_whatsapp_messages_since(
            last_timestamp=last_timestamp,
            last_source_id=last_source_id,
            batch_size=batch_size,
        )
    
    def _message_text_for_analysis(self, row):
        clean = getattr(row, "clean_content", None)
        if clean and str(clean).strip():
            return clean

        extracted = getattr(row, "extracted_text", None)
        if extracted and str(extracted).strip():
            return extracted

        return None

    def get_row_cursor_timestamp(self, row, row_result=None):
        return getattr(row, "message_time", None) or getattr(row, "synced_at", None)

    def _derive_delivery_status(self, row):
        """
        WhatsApp transport states like sent/read are useful, but event_fact.event_status
        is constrained to a smaller enum in Postgres. Keep the transport detail in meta,
        and store a DB-safe normalized event_status separately.
        """
        if getattr(row, "isread", None):
            return "read"
        if getattr(row, "isdelivered", None):
            return "delivered"
        if getattr(row, "issent", None):
            return "sent"
        return None

    def _build_event_meta(self, row, *, event_time, event_direction, delivery_status):
        message_text = self._message_text_for_analysis(row)

        preview = build_whatsapp_message_preview(
            clean_content=message_text,
            message_type=getattr(row, "message_type", None),
        )
        customer_phone, admin_phone, _executive_ref = self.source.extract_whatsapp_participants(row)
        thread_key = self.source.build_whatsapp_thread_key(
            row=row,
            customer_phone=customer_phone,
            admin_phone=admin_phone,
            remote_jid=getattr(row, "remote_jid", None),
        )
        conversation_kind = self.source.infer_whatsapp_conversation_kind(
            row=row,
            remote_jid=getattr(row, "remote_jid", None),
            customer_phone=customer_phone,
            admin_phone=admin_phone,
        )
        message_ontology = detect_whatsapp_message_ontology(
        clean_content=message_text,
        direction=event_direction,
        message_type=getattr(row, "message_type", None),
        remote_jid=getattr(row, "remote_jid", None),
        )
        message_ontology = _apply_property_share_guard(
        message_ontology,
        clean_content=message_text,
        message_type=getattr(row, "message_type", None),
        direction=event_direction,
        )
        if conversation_kind and message_ontology.get("conversation_kind") in (None, "", "unknown"):
            message_ontology["conversation_kind"] = conversation_kind

        event_meta = {
            "source_id": getattr(row, "source_id", None),
            "lead_id": getattr(row, "lead_id", None),
            "executive_id": getattr(row, "executive_id", None),
            "admin_number": getattr(row, "admin_number", None),
            "cx_number": getattr(row, "cx_number", None),
            "direction": getattr(row, "direction", None),
            "message_type": getattr(row, "message_type", None),
            "remote_jid": getattr(row, "remote_jid", None),
            "isread": getattr(row, "isread", None),
            "issent": getattr(row, "issent", None),
            "extracted_text_present": bool(getattr(row, "extracted_text", None)),
            "content_source": (
                "clean_content" if getattr(row, "clean_content", None)
                else "extracted_text" if getattr(row, "extracted_text", None)
                else None
            ),
            "isdelivered": getattr(row, "isdelivered", None),
            "delivery_status": delivery_status,
            "content_preview": preview,
            "thread_key": thread_key,
            "conversation_key": thread_key,
            "conversation_kind": conversation_kind,
            "message_ontology": message_ontology,
            "event_time": event_time,
        }
        return {k: v for k, v in event_meta.items() if v is not None}

    def _build_contexts(self, row, *, event_meta):
        contexts = list(self.source.extract_whatsapp_contexts(row))
        ontology = event_meta.get("message_ontology") or {}
        thread_key = event_meta.get("thread_key") or event_meta.get("conversation_key")
        conversation_kind = event_meta.get("conversation_kind")

        for context_type, value in (
            ("conversation", thread_key),
            ("thread_key", thread_key),
            ("conversation_kind", conversation_kind),
            ("topic_primary", ontology.get("topic_primary")),
            ("intent_primary", ontology.get("intent_primary")),
            ("journey_stage", ontology.get("journey_stage")),
            ("resolution_stage", ontology.get("resolution_stage")),
            ("speech_act", ontology.get("speech_act")),
        ):
            if value not in (None, ""):
                contexts.append((context_type, str(value)))
        return contexts

    def process_row(self, r):
        event_time = getattr(r, "message_time", None) or getattr(r, "synced_at", None)
        if not event_time:
            return {"processed": 1, "cursor_source_id": r.source_id}

        event_direction = normalize_message_direction(getattr(r, "direction", None))
        event_name = "whatsapp_outbound" if event_direction == "outbound" else "whatsapp_inbound"
        delivery_status = self._derive_delivery_status(r)
        event_meta = self._build_event_meta(
            r,
            event_time=event_time,
            event_direction=event_direction,
            delivery_status=delivery_status,
        )

        metric_value = getattr(r, "content_length", None)
        event_id = self.events.create_event(
            event_family="communication",
            event_name=event_name,
            event_channel="whatsapp",
            event_direction=event_direction,
            event_time=event_time,
            event_end_time=None,
            # Store the transport-specific sent/read/delivered detail in meta.
            # The event row itself is a completed communication occurrence.
            event_status="completed",
            source_table=self.SOURCE_TABLE_NAME,
            source_id=str(r.source_id),
            metric_value=metric_value,
            metric_unit="count" if metric_value is not None else None,
            metric_name="content_length" if metric_value is not None else None,
            meta=event_meta,
        )

        participants_written = 0
        contexts_written = 0
        created_events = 1 if event_id else 0

        if event_id:
            customer_phone, admin_phone, executive_ref = self.source.extract_whatsapp_participants(r)
            seq = 1
            if event_direction == "outbound":
                seq, added = add_phone_participant(
                    self,
                    event_id=event_id,
                    participant_seq=seq,
                    phone=admin_phone,
                    role="admin_number",
                    direction_role="from",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                participants_written += added
                seq, added = add_phone_participant(
                    self,
                    event_id=event_id,
                    participant_seq=seq,
                    phone=customer_phone,
                    role="customer",
                    direction_role="to",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                participants_written += added
            else:
                seq, added = add_phone_participant(
                    self,
                    event_id=event_id,
                    participant_seq=seq,
                    phone=customer_phone,
                    role="customer",
                    direction_role="from",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                participants_written += added
                seq, added = add_phone_participant(
                    self,
                    event_id=event_id,
                    participant_seq=seq,
                    phone=admin_phone,
                    role="admin_number",
                    direction_role="to",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                participants_written += added

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
            contexts_written += append_contexts(self, event_id, self._build_contexts(r, event_meta=event_meta))

        return {
            "processed": 1,
            "cursor_source_id": r.source_id,
            "created_events": created_events,
            "participants_written": participants_written,
            "contexts_written": contexts_written,
        }

    def run(self, batch_size=3000, limit=None, start_source_id=None):
        result = super().run(batch_size=batch_size, limit=limit, start_source_id=start_source_id)
        return {
            "processed_whatsapp_rows": result["processed_rows"],
            "created_events": result.get("created_events", 0),
            "participants_written": result.get("participants_written", 0),
            "contexts_written": result.get("contexts_written", 0),
            "last_source_id": result["last_source_id"],
        }

