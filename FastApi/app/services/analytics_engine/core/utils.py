import re
from collections import Counter
from datetime import datetime, timedelta


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_WHITESPACE_RE = re.compile(r"\s+")
_AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*\d+(?:[\.,]\d{1,2})?", re.IGNORECASE)
_PLUS_CODE_RE = re.compile(r"\b[A-Z0-9]{4}\+[A-Z0-9]{2,4}\b")
_QUESTION_START_RE = re.compile(
    r"^(please|pls|plz|can|could|when|what|why|how|where|share|send|confirm|check|update)\b",
    re.IGNORECASE,
)

_ACK_TOKENS = {
    "ok",
    "okay",
    "kk",
    "k",
    "done",
    "noted",
    "fine",
    "sure",
    "thanks",
    "thank you",
    "thx",
    "received",
    "👍",
    "👍👍",
    "yes",
    "ya",
    "yaa",
    "yep",
}

_GREETING_TOKENS = {"hi", "hello", "hey", "good morning", "good evening", "good afternoon"}

_PAYMENT_TERMS = {
    "payment",
    "pay",
    "amount",
    "balance",
    "refund",
    "account",
    "invoice",
    "bill",
    "receipt",
    "utr",
    "upi",
    "transfer",
    "original",
    "proof",
    "settlement",
    "difference amount",
}

_LOCATION_TERMS = {
    "address",
    "location",
    "maps",
    "gmap",
    "google map",
    "bengaluru",
    "bangalore",
    "floor",
    "room no",
    "flat number",
    "flat no",
    "unit",
    "building",
    "residency",
}

_LOGISTICS_TERMS = {
    "tempo",
    "freight",
    "transport",
    "shift",
    "shifting",
    "loading",
    "unloading",
    "truck",
    "driver",
    "vehicle",
    "pickup",
    "drop",
}

_AGREEMENT_TERMS = {
    "agreement",
    "contract",
    "continue",
    "continuing",
    "renewal",
    "renew",
    "lease",
    "rent agreement",
}

_HANDOVER_TERMS = {
    "handover",
    "vacated",
    "vacate",
    "ready",
    "readiness",
    "move in",
    "move-in",
    "move out",
    "move-out",
    "check in",
    "check-in",
    "check out",
    "check-out",
    "keys",
    "room no",
    "flat number",
    "door mat",
}

_SUPPORT_TERMS = {
    "issue",
    "problem",
    "inconvenience",
    "not working",
    "repair",
    "maintenance",
    "leak",
    "broken",
    "water tanker",
    "complaint",
    "delay",
}

_REFUSAL_TERMS = {
    "do not want",
    "don't want",
    "dont want",
    "not want to continue",
    "cannot continue",
    "can't continue",
    "cancel",
    "cancelled",
    "stop",
}

_HIGH_URGENCY_TERMS = {
    "urgent",
    "immediately",
    "asap",
    "today itself",
    "right now",
    "problem",
    "issue",
    "refund",
    "cancel",
}


def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None

    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 10:
        digits = "91" + digits

    return digits or None



def normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    return str(email).strip().lower() or None



def extract_emails(value: str | None) -> list[str]:
    if not value:
        return []

    matches = _EMAIL_RE.findall(str(value))
    if not matches:
        normalized = normalize_email(value)
        return [normalized] if normalized and "@" in normalized else []

    out: list[str] = []
    seen: set[str] = set()
    for item in matches:
        email = normalize_email(item)
        if email and email not in seen:
            seen.add(email)
            out.append(email)
    return out



def normalize_free_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\u200b", " ").strip()
    text = _WHITESPACE_RE.sub(" ", text)
    return text or None



def truncate_text(value: str | None, max_length: int = 250) -> str | None:
    text = normalize_free_text(value)
    if not text:
        return None
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."



def normalize_event_direction(
    call_direction: str | None,
    call_result: str | None = None,
) -> str | None:
    d = str(call_direction).strip().lower() if call_direction else ""
    r = str(call_result).strip().lower() if call_result else ""

    mapping = {
        "incoming": "inbound",
        "incoming call": "inbound",
        "inbound": "inbound",
        "outgoing": "outbound",
        "outgoing call": "outbound",
        "outbound": "outbound",
        "internal": "internal",
        "system": "system",
        "missed": "inbound",
        "rejected": "inbound",
        "declined": "inbound",
        "received": "inbound",
        "sent": "outbound",
    }

    if d in mapping:
        return mapping[d]

    if r == "missed":
        return "inbound"

    if d == "unknown":
        return None

    return None



def normalize_call_status(value: str | None) -> str:
    if not value:
        return "unknown"

    v = str(value).strip().lower()

    mapping = {
        "connected": "completed",
        "answered": "completed",
        "completed": "completed",
        "missed": "missed",
        "busy": "missed",
        "not connected": "missed",
        "not_connected": "missed",
        "no answer": "missed",
        "no_answer": "missed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "failed": "unknown",
        "error": "unknown",
        "unknown": "unknown",
    }

    return mapping.get(v, "unknown")



def compute_event_end_time(event_time, duration_seconds):
    if not event_time or duration_seconds in (None, ""):
        return None

    try:
        seconds = int(duration_seconds)
    except Exception:
        return None

    if seconds < 0:
        return None

    return event_time + timedelta(seconds=seconds)



def infer_whatsapp_message_kind(message_type: str | None, clean_content: str | None = None) -> str:
    raw_type = str(message_type or "").strip().lower()
    text = normalize_free_text(clean_content)

    mapping = {
        "text": "text",
        "conversation": "text",
        "extended_text": "text",
        "extendedtextmessage": "text",
        "image": "image",
        "imagemessage": "image",
        "video": "video",
        "videomessage": "video",
        "document": "document",
        "documentmessage": "document",
        "audio": "audio",
        "audiomessage": "audio",
        "voice": "audio",
        "ptt": "audio",
        "sticker": "sticker",
        "stickermessage": "sticker",
        "location": "location",
        "locationmessage": "location",
        "live_location": "location",
        "contact": "contact",
        "contactmessage": "contact",
        "contactsarraymessage": "contact",
        "reaction": "reaction",
        "reactionmessage": "reaction",
    }

    if raw_type in mapping:
        kind = mapping[raw_type]
    elif raw_type:
        if "image" in raw_type:
            kind = "image"
        elif "video" in raw_type:
            kind = "video"
        elif "document" in raw_type:
            kind = "document"
        elif "audio" in raw_type or "voice" in raw_type or "ptt" in raw_type:
            kind = "audio"
        elif "sticker" in raw_type:
            kind = "sticker"
        elif "location" in raw_type:
            kind = "location"
        elif "contact" in raw_type:
            kind = "contact"
        else:
            kind = raw_type[:32]
    elif text:
        kind = "text"
    else:
        kind = "empty"

    if kind == "empty" and text:
        return "text"
    if kind == "text" and not text:
        return "media_only"
    return kind



def build_whatsapp_message_preview(
    clean_content: str | None,
    message_type: str | None = None,
    max_length: int = 250,
) -> str:
    text = truncate_text(clean_content, max_length=max_length)
    if text:
        return text

    kind = infer_whatsapp_message_kind(message_type=message_type, clean_content=clean_content)
    if kind and kind not in {"text", "empty"}:
        return f"[{kind}]"
    return "-"



def build_whatsapp_conversation_key(
    customer_phone: str | None = None,
    admin_phone: str | None = None,
    remote_jid: str | None = None,
) -> str | None:
    remote = normalize_free_text(remote_jid)
    customer = normalize_phone(customer_phone)
    admin = normalize_phone(admin_phone)

    if remote:
        return f"jid:{remote}"
    if admin and customer:
        return f"wa:{admin}->{customer}"
    if customer:
        return f"wa:{customer}"
    if admin:
        return f"wa:{admin}"
    return None



def _contains_any(text: str, terms: set[str]) -> bool:
    if not text:
        return False
    for term in terms:
        if term in text:
            return True
    return False



def _is_ack_message(text: str) -> bool:
    if not text:
        return False
    normalized = text.strip().lower()
    if normalized in _ACK_TOKENS:
        return True
    if len(normalized.split()) <= 3 and normalized in _ACK_TOKENS:
        return True
    return False



def _is_greeting(text: str) -> bool:
    if not text:
        return False
    normalized = text.strip().lower()
    return normalized in _GREETING_TOKENS



def _safe_confidence(high: bool, medium: bool = False, default: float = 0.35) -> float:
    if high:
        return 0.9
    if medium:
        return 0.7
    return default



def detect_whatsapp_message_ontology(
    clean_content: str | None,
    direction: str | None,
    message_type: str | None = None,
    remote_jid: str | None = None,
) -> dict:
    text = normalize_free_text(clean_content) or ""
    lowered = text.lower()
    message_kind = infer_whatsapp_message_kind(message_type=message_type, clean_content=clean_content)
    conversation_kind = "group" if str(remote_jid or "").endswith("@g.us") else "direct"

    contains_amount = bool(_AMOUNT_RE.search(lowered))
    contains_question = "?" in text or bool(_QUESTION_START_RE.match(text))
    contains_payment = _contains_any(lowered, _PAYMENT_TERMS) or (
        contains_amount and _contains_any(lowered, {"include", "extra", "difference", "balance", "less", "more"})
    )
    contains_location = _contains_any(lowered, _LOCATION_TERMS) or bool(_PLUS_CODE_RE.search(text))
    contains_logistics = _contains_any(lowered, _LOGISTICS_TERMS)
    contains_agreement = _contains_any(lowered, _AGREEMENT_TERMS)
    contains_handover = _contains_any(lowered, _HANDOVER_TERMS)
    contains_support = _contains_any(lowered, _SUPPORT_TERMS)
    contains_refusal = _contains_any(lowered, _REFUSAL_TERMS)
    is_ack = _is_ack_message(text)
    is_greeting = _is_greeting(text)
    contains_urgency = _contains_any(lowered, _HIGH_URGENCY_TERMS)

    requires_reply = False
    is_actionable = False
    speech_act = "update"
    intent_primary = "share_update"
    topic_primary = "general"

    if message_kind not in {"text", "empty"} and not text:
        speech_act = "media_share"
        intent_primary = "share_information"
        topic_primary = "document_media"
    elif is_ack:
        speech_act = "acknowledgment"
        intent_primary = "acknowledge"
        topic_primary = "general"
    elif contains_refusal:
        speech_act = "refusal"
        intent_primary = "decline"
        topic_primary = "agreement_contract" if contains_agreement else "general"
        requires_reply = direction == "inbound"
        is_actionable = requires_reply
    elif contains_support:
        speech_act = "complaint"
        intent_primary = "report_issue"
        topic_primary = "support_issue"
        requires_reply = direction == "inbound"
        is_actionable = requires_reply
    elif contains_question or lowered.startswith(("please ", "pls ", "plz ", "can ", "could ", "when ", "what ", "why ", "how ", "share ", "send ", "confirm ", "check ", "update ")):
        speech_act = "request"
        requires_reply = direction == "inbound"
        is_actionable = requires_reply
        if contains_payment:
            topic_primary = "payment"
            if _contains_any(lowered, {"receipt", "invoice", "bill", "proof", "original", "utr", "screenshot"}):
                intent_primary = "request_document"
            elif _contains_any(lowered, {"refund", "balance", "difference", "account"}):
                intent_primary = "request_payment_clarification"
            else:
                intent_primary = "payment_follow_up"
        elif contains_handover:
            topic_primary = "handover_readiness"
            intent_primary = "request_readiness_update"
        elif contains_location:
            topic_primary = "location_property"
            intent_primary = "request_location_information"
        elif contains_logistics:
            topic_primary = "logistics"
            intent_primary = "request_logistics_update"
        elif contains_agreement:
            topic_primary = "agreement_contract"
            intent_primary = "request_contract_clarification"
        else:
            topic_primary = "general"
            intent_primary = "request_information"
    elif contains_payment:
        topic_primary = "payment"
        speech_act = "update"
        if _contains_any(lowered, {"receipt", "invoice", "bill", "proof", "original", "utr", "screenshot"}):
            intent_primary = "share_payment_proof" if direction == "outbound" else "request_document"
            speech_act = "share_information" if direction == "outbound" else "follow_up"
        elif _contains_any(lowered, {"refund", "balance", "difference", "account", "sent to account"}):
            intent_primary = "share_payment_update"
        elif _contains_any(lowered, {"include", "add", "extra", "less", "more"}) and contains_amount:
            intent_primary = "revise_amount"
        else:
            intent_primary = "payment_follow_up"
        requires_reply = direction == "inbound" and speech_act in {"follow_up", "update"}
        is_actionable = requires_reply
    elif contains_handover:
        topic_primary = "handover_readiness"
        speech_act = "update"
        intent_primary = "share_readiness_update"
        requires_reply = direction == "inbound" and _contains_any(lowered, {"will", "update", "ready", "vacated"})
        is_actionable = requires_reply
    elif contains_location:
        topic_primary = "location_property"
        speech_act = "share_information"
        intent_primary = "share_location"
    elif contains_logistics:
        topic_primary = "logistics"
        speech_act = "update"
        intent_primary = "share_logistics_update"
        requires_reply = direction == "inbound" and contains_amount
        is_actionable = requires_reply
    elif contains_agreement:
        topic_primary = "agreement_contract"
        speech_act = "update"
        intent_primary = "share_contract_update"
        requires_reply = direction == "inbound"
        is_actionable = requires_reply
    elif is_greeting:
        speech_act = "greeting"
        intent_primary = "start_conversation"
        topic_primary = "general"
        requires_reply = direction == "inbound"
        is_actionable = requires_reply
    else:
        speech_act = "update"
        intent_primary = "share_update"
        topic_primary = "general"
        requires_reply = direction == "inbound" and len(lowered.split()) > 8
        is_actionable = requires_reply

    if topic_primary == "agreement_contract":
        journey_stage = "pre_commitment"
    elif topic_primary in {"handover_readiness", "logistics", "location_property"}:
        journey_stage = "fulfillment"
    elif topic_primary == "support_issue":
        journey_stage = "active_service"
    elif topic_primary == "payment":
        journey_stage = "commercial_closure"
    else:
        journey_stage = "general_engagement"

    if speech_act in {"acknowledgment", "media_share"} and not requires_reply:
        resolution_stage = "informational"
    elif direction == "inbound" and requires_reply:
        resolution_stage = "awaiting_internal"
    elif direction == "outbound" and speech_act in {"request", "follow_up"}:
        resolution_stage = "awaiting_customer"
    elif speech_act in {"share_information", "update", "greeting"} and not requires_reply:
        resolution_stage = "informational"
    else:
        resolution_stage = "new"

    if contains_urgency or speech_act in {"complaint", "refusal"}:
        urgency = "high"
    elif requires_reply or topic_primary in {"payment", "handover_readiness", "logistics", "support_issue"}:
        urgency = "medium"
    else:
        urgency = "low"

    topic_is_specific = topic_primary != "general"
    intent_is_specific = intent_primary not in {"share_update", "request_information", "acknowledge"}
    speech_is_specific = speech_act in {"request", "complaint", "refusal", "acknowledgment", "media_share", "share_information"}

    return {
        "channel": "whatsapp",
        "direction": direction,
        "conversation_kind": conversation_kind,
        "message_kind": message_kind,
        "speech_act": speech_act,
        "topic_primary": topic_primary,
        "intent_primary": intent_primary,
        "journey_stage": journey_stage,
        "resolution_stage": resolution_stage,
        "urgency": urgency,
        "requires_reply": requires_reply,
        "is_actionable": is_actionable,
        "is_ack": is_ack,
        "contains_amount": contains_amount,
        "contains_payment_reference": contains_payment,
        "contains_location_reference": contains_location,
        "contains_logistics_reference": contains_logistics,
        "contains_question": contains_question,
        "contains_support_reference": contains_support,
        "confidence": {
            "channel": 0.99,
            "direction": 0.98 if direction in {"inbound", "outbound"} else 0.5,
            "message_kind": 0.95 if message_kind not in {"empty", "media_only"} else 0.75,
            "speech_act": _safe_confidence(speech_is_specific, medium=speech_act == "update", default=0.45),
            "topic_primary": _safe_confidence(topic_is_specific, medium=contains_amount or contains_question, default=0.3),
            "intent_primary": _safe_confidence(intent_is_specific, medium=contains_question or contains_amount, default=0.4),
            "journey_stage": 0.78 if journey_stage != "general_engagement" else 0.42,
            "resolution_stage": 0.84 if requires_reply or resolution_stage == "informational" else 0.55,
            "urgency": 0.82 if urgency == "high" else 0.68 if urgency == "medium" else 0.5,
        },
        "tagged_from": "rule_engine_v2_message",
    }



def _normalize_counter_key(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None



def _pick_dominant(counter: Counter, fallback: str = "general") -> str:
    if not counter:
        return fallback
    for key, _count in counter.most_common():
        if key and key not in {"general", "share_update", "update", "informational"}:
            return key
    return counter.most_common(1)[0][0] or fallback



def _safe_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None



def humanize_label(value: str | None) -> str:
    if not value:
        return "-"
    return str(value).replace("_", " ").strip().title()



def summarize_whatsapp_turn(turn: dict, preview_limit: int = 3) -> str:
    previews: list[str] = []
    seen: set[str] = set()
    for message in turn.get("messages", []):
        preview = message.get("preview")
        if not preview or preview == "-":
            continue
        if preview in seen:
            continue
        seen.add(preview)
        previews.append(preview)
        if len(previews) >= preview_limit:
            break

    topic = humanize_label(turn.get("topic_primary"))
    if previews:
        return f"{topic} — {' | '.join(previews)}"
    return topic



def _derive_turn_state(turn: dict) -> str:
    last_message = turn.get("last_message") or {}
    ontology = last_message.get("message_ontology") or {}
    latest_direction = turn.get("latest_direction")

    if latest_direction == "inbound" and ontology.get("requires_reply"):
        return "awaiting_internal_reply"
    if latest_direction == "outbound" and (ontology.get("speech_act") in {"request", "follow_up"}):
        return "awaiting_customer"
    return "informational"



def build_whatsapp_turns(
    messages: list[dict],
    *,
    max_gap_minutes: int = 45,
    topic_break_minutes: int = 15,
) -> list[dict]:
    ordered = sorted(
        messages,
        key=lambda row: (
            _safe_datetime(row.get("event_time") or row.get("message_time")) or datetime.min,
            str(row.get("source_id") or ""),
        ),
    )

    turns: list[dict] = []
    current: dict | None = None
    previous_time: datetime | None = None
    previous_topic: str | None = None

    for row in ordered:
        row_time = _safe_datetime(row.get("event_time") or row.get("message_time"))
        ontology = row.get("message_ontology") or {}
        row_topic = ontology.get("topic_primary") or "general"

        start_new = False
        if current is None:
            start_new = True
        elif row_time and previous_time:
            gap_minutes = (row_time - previous_time).total_seconds() / 60.0
            if gap_minutes > max_gap_minutes:
                start_new = True
            elif previous_topic and row_topic != previous_topic and row_topic != "general" and gap_minutes > topic_break_minutes:
                start_new = True

        if start_new:
            if current is not None:
                current["state"] = _derive_turn_state(current)
                current["summary"] = summarize_whatsapp_turn(current)
                turns.append(current)
            current = {
                "start_time": row_time,
                "end_time": row_time,
                "messages": [],
                "topic_counts": Counter(),
                "intent_counts": Counter(),
                "speech_counts": Counter(),
                "direction_counts": Counter(),
                "latest_direction": None,
                "last_message": None,
            }

        current["messages"].append(row)
        current["end_time"] = row_time or current.get("end_time")
        direction = row.get("event_direction") or row.get("direction") or ontology.get("direction") or "unknown"
        current["direction_counts"][_normalize_counter_key(direction) or "unknown"] += 1
        current["topic_counts"][_normalize_counter_key(row_topic) or "general"] += 1
        current["intent_counts"][_normalize_counter_key(ontology.get("intent_primary")) or "share_update"] += 1
        current["speech_counts"][_normalize_counter_key(ontology.get("speech_act")) or "update"] += 1
        current["latest_direction"] = direction
        current["last_message"] = row

        previous_time = row_time or previous_time
        previous_topic = row_topic

    if current is not None:
        current["state"] = _derive_turn_state(current)
        current["summary"] = summarize_whatsapp_turn(current)
        turns.append(current)

    for idx, turn in enumerate(turns, start=1):
        turn["turn_id"] = idx
        last_ontology = ((turn.get("last_message") or {}).get("message_ontology") or {})
        turn["topic_primary"] = _pick_dominant(turn.get("topic_counts", Counter()), fallback=last_ontology.get("topic_primary") or "general")
        turn["intent_primary"] = last_ontology.get("intent_primary") or _pick_dominant(
            turn.get("intent_counts", Counter()),
            fallback="share_update",
        )
        turn["speech_act"] = last_ontology.get("speech_act") or _pick_dominant(
            turn.get("speech_counts", Counter()),
            fallback="update",
        )
        turn["state"] = _derive_turn_state(turn)
        turn["summary"] = summarize_whatsapp_turn(turn)

    return turns



def build_whatsapp_conversation_summary(turns: list[dict]) -> dict:
    if not turns:
        return {
            "current_state": "no_messages",
            "next_best_action": "review_conversation",
            "recent_about": [],
            "open_asks": [],
            "topic_blend": {},
            "latest_turn": None,
        }

    recent_turns = turns[-5:]
    latest_turn = turns[-1]
    topic_counts = Counter(turn.get("topic_primary") or "general" for turn in recent_turns)
    open_turns = [turn for turn in recent_turns if turn.get("state") == "awaiting_internal_reply"]

    if latest_turn.get("state") == "awaiting_internal_reply":
        current_state = "awaiting_internal_reply"
    elif latest_turn.get("state") == "awaiting_customer":
        current_state = "awaiting_customer"
    else:
        current_state = "informational"

    latest_topic = latest_turn.get("topic_primary") or "general"
    latest_intent = latest_turn.get("intent_primary") or "share_update"

    if current_state == "awaiting_internal_reply":
        if latest_topic == "payment":
            next_best_action = "reply_with_payment_clarification"
        elif latest_topic == "handover_readiness":
            next_best_action = "reply_with_readiness_update"
        elif latest_topic == "logistics":
            next_best_action = "reply_with_logistics_confirmation"
        elif latest_topic == "agreement_contract":
            next_best_action = "clarify_contract_status"
        elif latest_topic == "support_issue":
            next_best_action = "acknowledge_issue_and_assign"
        elif latest_topic == "location_property":
            next_best_action = "share_location_or_property_details"
        else:
            next_best_action = "reply_with_information"
    elif current_state == "awaiting_customer":
        next_best_action = "wait_for_customer_response"
    else:
        next_best_action = "monitor_conversation"

    return {
        "current_state": current_state,
        "next_best_action": next_best_action,
        "recent_about": [humanize_label(topic) for topic, _count in topic_counts.most_common(3)],
        "open_asks": [turn.get("summary") for turn in open_turns[-3:]],
        "topic_blend": {humanize_label(topic): count for topic, count in topic_counts.most_common()},
        "latest_turn": latest_turn,
        "latest_topic": humanize_label(latest_topic),
        "latest_intent": humanize_label(latest_intent),
    }
