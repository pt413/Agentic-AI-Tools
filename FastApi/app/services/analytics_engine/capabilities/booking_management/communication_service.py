from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.model.message import Message


ConversationType = Literal["all", "inbound", "outbound"]

_CUSTOMER_DIRECTIONS = {"incoming", "inbound", "received", "from_customer", "customer", "in"}
_STAFF_DIRECTIONS = {"outgoing", "outbound", "sent", "to_customer", "staff", "sales", "admin", "user", "out"}
_SYSTEM_MESSAGE_PATTERNS = (
    re.compile(r"\b(this is an automated message|automated message|no[- ]?reply|do not reply)\b", re.I),
    re.compile(r"\b(one[- ]time password|otp|verification code)\b", re.I),
    re.compile(r"\b(unsubscribe|opt out|stop to unsubscribe)\b", re.I),
)
_LLM_REVIEW_TEMPLATE = """Act as a WhatsApp sales conversation reviewer.

Rules:
- Review exactly one customer thread.
- Treat `deterministic_facts` as ground truth for counts, delay, and reply-gap facts.
- Treat `deterministic_rating` as the baseline operational score for responsiveness and closure.
- Use only the provided messages and facts. Do not invent missing context.
- If `deterministic_facts.score_status` is `no_score`, do not force a numeric quality score.

Return JSON only with this shape:
{
  "sales_stage": "new_lead | requirement_collection | property_suggestion | visit_planning | negotiation | booking_discussion | post_booking_followup | unclear",
  "customer_need": "short sentence",
  "staff_handling": "short sentence",
  "status": "scored | follow_up_needed | no_score",
  "quality_score": 1-10 or null,
  "priority_score": 1-10 or null,
  "reason": "1-2 short sentences",
  "evidence": [
    {
      "message_idx": 1,
      "why": "why this message matters"
    }
  ],
  "next_action": "single next best action"
}

CONVERSATION DATA:
{conversation_json}
"""


def _direction_values(conversation_type: ConversationType) -> list[str] | None:
    normalized = str(conversation_type or "all").strip().lower()
    if normalized == "inbound":
        return ["incoming", "inbound"]
    if normalized == "outbound":
        return ["outgoing", "outbound"]
    return None


def get_whatsapp_conversations(
    db: Session,
    *,
    admin_number: str,
    start_timestamp: datetime,
    end_timestamp: datetime | None = None,
    conversation_type: ConversationType = "all",
    device: str = "baileys",
    cx_prefix: str = "91",
    cx_number: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch WhatsApp messages for the exact conversation query shape requested."""
    query = (
        db.query(
            Message.admin_number,
            Message.cx_number,
            Message.content,
            Message.direction,
            Message.timestamp,
        )
       
        .filter(Message.admin_number == admin_number)
        .filter(Message.timestamp > start_timestamp)
    )

    cx_number_text = str(cx_number or "").strip()
    if cx_number_text:
        query = query.filter(Message.cx_number == cx_number_text)
    else:
        query = query.filter(Message.cx_number.like(f"{cx_prefix}%"))

    direction_values = _direction_values(conversation_type)
    if direction_values:
        query = query.filter(Message.direction.in_(direction_values))

    if end_timestamp is not None:
        query = query.filter(Message.timestamp <= end_timestamp)

    rows = query.order_by(Message.cx_number.asc(), Message.timestamp.asc()).all()
    return [
        {
            "admin_number": row.admin_number,
            "cx_number": row.cx_number,
            "content": row.content,
            "direction": row.direction,
            "timestamp": row.timestamp,
        }
        for row in rows
    ]


def normalize_conversation_speaker(direction: Any) -> Literal["customer", "staff", "unknown"]:
    normalized = str(direction or "").strip().lower()
    if normalized in _CUSTOMER_DIRECTIONS:
        return "customer"
    if normalized in _STAFF_DIRECTIONS:
        return "staff"
    return "unknown"


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _truncate_text(value: Any, *, max_chars: int) -> str:
    text = _normalized_text(value)
    if not text:
        return ""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _is_system_like_message(content: Any) -> bool:
    text = _normalized_text(content)
    if not text:
        return False
    return any(pattern.search(text) for pattern in _SYSTEM_MESSAGE_PATTERNS)


def _minutes_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return int((end - start).total_seconds() // 60)


def _seconds_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return int((end - start).total_seconds())


def _clamp_score(value: int, *, low: int = 1, high: int = 10) -> int:
    return max(low, min(high, int(value)))


def _humanize_reason(code: Any) -> str:
    return str(code or "").replace("_", " ").strip()


def build_whatsapp_thread_facts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered_rows = sorted(rows, key=lambda row: row.get("timestamp") or datetime.min)
    message_count = len(ordered_rows)
    customer_message_count = 0
    staff_message_count = 0
    empty_content_messages = 0
    non_empty_messages = 0
    system_like_messages = 0
    unknown_direction_messages = 0

    first_customer_message_at: datetime | None = None
    first_staff_reply_at: datetime | None = None
    last_message_at: datetime | None = None
    last_sender = "unknown"
    pending_customer_started_at: datetime | None = None
    pending_customer_messages = 0
    longest_customer_wait_seconds: int | None = None

    for row in ordered_rows:
        timestamp = row.get("timestamp")
        speaker = normalize_conversation_speaker(row.get("direction"))
        content_text = _normalized_text(row.get("content"))

        if content_text:
            non_empty_messages += 1
            if _is_system_like_message(content_text):
                system_like_messages += 1
        else:
            empty_content_messages += 1

        if speaker == "customer":
            customer_message_count += 1
            if first_customer_message_at is None:
                first_customer_message_at = timestamp
            if pending_customer_started_at is None:
                pending_customer_started_at = timestamp
            pending_customer_messages += 1
        elif speaker == "staff":
            staff_message_count += 1
            if pending_customer_started_at is not None:
                gap_seconds = _seconds_between(pending_customer_started_at, timestamp)
                if gap_seconds is not None:
                    longest_customer_wait_seconds = max(longest_customer_wait_seconds or 0, gap_seconds)
                if first_staff_reply_at is None:
                    first_staff_reply_at = timestamp
                pending_customer_started_at = None
                pending_customer_messages = 0
        else:
            unknown_direction_messages += 1

        if timestamp is not None:
            last_message_at = timestamp
        last_sender = speaker

    ineligible_reasons: list[str] = []
    if customer_message_count == 0:
        ineligible_reasons.append("no_customer_message")
    if customer_message_count > 0 and first_staff_reply_at is None:
        ineligible_reasons.append("no_staff_reply")
    if non_empty_messages == 0:
        ineligible_reasons.append("empty_content_only")
    if unknown_direction_messages > 0:
        ineligible_reasons.append("direction_unclear")
    if non_empty_messages > 0 and system_like_messages == non_empty_messages:
        ineligible_reasons.append("system_or_automation_only")

    return {
        "message_count": message_count,
        "customer_message_count": customer_message_count,
        "staff_message_count": staff_message_count,
        "first_customer_message_at": first_customer_message_at,
        "first_staff_reply_at": first_staff_reply_at,
        "first_response_delay_minutes": _minutes_between(first_customer_message_at, first_staff_reply_at),
        "first_response_delay_seconds": _seconds_between(first_customer_message_at, first_staff_reply_at),
        "longest_customer_wait_minutes": None
        if longest_customer_wait_seconds is None
        else int(longest_customer_wait_seconds // 60),
        "longest_customer_wait_seconds": longest_customer_wait_seconds,
        "last_sender": last_sender,
        "last_message_at": last_message_at,
        "unanswered_customer_messages": pending_customer_messages,
        "has_unanswered_customer_message": pending_customer_messages > 0,
        "empty_content_messages": empty_content_messages,
        "non_empty_messages": non_empty_messages,
        "system_like_messages": system_like_messages,
        "unknown_direction_messages": unknown_direction_messages,
        "score_status": "eligible" if not ineligible_reasons else "no_score",
        "ineligible_reasons": ineligible_reasons,
    }


def build_whatsapp_thread_rating(facts: dict[str, Any]) -> dict[str, Any]:
    ineligible_reasons = list(facts.get("ineligible_reasons") or [])
    if str(facts.get("score_status") or "").strip().lower() != "eligible":
        reason_text = ", ".join(_humanize_reason(reason) for reason in ineligible_reasons) or "not enough evidence"
        return {
            "status": "no_score",
            "quality_score": None,
            "priority_score": None,
            "rating_label": "no_score",
            "reason": f"No deterministic score because {reason_text}.",
            "breakdown": [],
        }

    score = 10
    breakdown: list[dict[str, Any]] = []

    first_response_delay_minutes = facts.get("first_response_delay_minutes")
    if first_response_delay_minutes is None:
        breakdown.append({"metric": "first_response_delay", "impact": 0, "detail": "First response time not visible."})
    elif first_response_delay_minutes <= 5:
        breakdown.append({"metric": "first_response_delay", "impact": 0, "detail": f"First response in {first_response_delay_minutes} minutes."})
    elif first_response_delay_minutes <= 15:
        score -= 1
        breakdown.append({"metric": "first_response_delay", "impact": -1, "detail": f"First response took {first_response_delay_minutes} minutes."})
    elif first_response_delay_minutes <= 60:
        score -= 2
        breakdown.append({"metric": "first_response_delay", "impact": -2, "detail": f"First response took {first_response_delay_minutes} minutes."})
    elif first_response_delay_minutes <= 240:
        score -= 3
        breakdown.append({"metric": "first_response_delay", "impact": -3, "detail": f"First response took {first_response_delay_minutes} minutes."})
    else:
        score -= 4
        breakdown.append({"metric": "first_response_delay", "impact": -4, "detail": f"First response took {first_response_delay_minutes} minutes."})

    longest_wait_minutes = facts.get("longest_customer_wait_minutes")
    if longest_wait_minutes is None:
        breakdown.append({"metric": "customer_wait_gap", "impact": 0, "detail": "No customer wait gap visible."})
    elif longest_wait_minutes <= 30:
        breakdown.append({"metric": "customer_wait_gap", "impact": 0, "detail": f"Longest customer wait gap was {longest_wait_minutes} minutes."})
    elif longest_wait_minutes <= 120:
        score -= 1
        breakdown.append({"metric": "customer_wait_gap", "impact": -1, "detail": f"Longest customer wait gap was {longest_wait_minutes} minutes."})
    elif longest_wait_minutes <= 720:
        score -= 2
        breakdown.append({"metric": "customer_wait_gap", "impact": -2, "detail": f"Longest customer wait gap was {longest_wait_minutes} minutes."})
    else:
        score -= 3
        breakdown.append({"metric": "customer_wait_gap", "impact": -3, "detail": f"Longest customer wait gap was {longest_wait_minutes} minutes."})

    unanswered_customer_messages = int(facts.get("unanswered_customer_messages") or 0)
    if unanswered_customer_messages <= 0:
        breakdown.append({"metric": "unanswered_customer_messages", "impact": 0, "detail": "No unanswered customer messages."})
    elif unanswered_customer_messages == 1:
        score -= 2
        breakdown.append({"metric": "unanswered_customer_messages", "impact": -2, "detail": "There is 1 unanswered customer message."})
    else:
        score -= 3
        breakdown.append({"metric": "unanswered_customer_messages", "impact": -3, "detail": f"There are {unanswered_customer_messages} unanswered customer messages."})

    score = _clamp_score(score)

    if unanswered_customer_messages >= 2:
        priority_score = 9
    elif unanswered_customer_messages == 1:
        priority_score = 8
    elif isinstance(longest_wait_minutes, int) and longest_wait_minutes > 240:
        priority_score = 7
    elif isinstance(first_response_delay_minutes, int) and first_response_delay_minutes > 60:
        priority_score = 6
    elif isinstance(first_response_delay_minutes, int) and first_response_delay_minutes > 15:
        priority_score = 4
    else:
        priority_score = 2

    if score >= 8:
        rating_label = "strong"
    elif score >= 6:
        rating_label = "fair"
    else:
        rating_label = "weak"

    if unanswered_customer_messages > 0:
        status = "follow_up_needed"
        reason = "Customer has unanswered messages and needs follow-up."
    elif isinstance(first_response_delay_minutes, int) and first_response_delay_minutes > 60:
        status = "scored"
        reason = "Thread is answered, but response speed was weak."
    else:
        status = "scored"
        reason = "Thread is answered and response timing looks operationally healthy."

    return {
        "status": status,
        "quality_score": score,
        "priority_score": priority_score,
        "rating_label": rating_label,
        "reason": reason,
        "breakdown": breakdown,
    }


def _thread_preview(rows: list[dict[str, Any]], *, max_chars: int = 80) -> str:
    for row in reversed(rows):
        preview = _truncate_text(row.get("content"), max_chars=max_chars)
        if preview:
            return preview
    return "No message content"


def build_whatsapp_thread_summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(rows, key=lambda item: ((item.get("cx_number") or ""), item.get("timestamp") or datetime.min)):
        cx_number = str(row.get("cx_number") or "").strip() or "unknown"
        grouped.setdefault(cx_number, []).append(row)

    summaries: list[dict[str, Any]] = []
    for cx_number, thread_rows in grouped.items():
        facts = build_whatsapp_thread_facts(thread_rows)
        rating = build_whatsapp_thread_rating(facts)
        summaries.append(
            {
                "admin_number": thread_rows[0].get("admin_number"),
                "cx_number": cx_number,
                "message_count": len(thread_rows),
                "last_message_at": facts.get("last_message_at"),
                "last_sender": facts.get("last_sender"),
                "last_preview": _thread_preview(thread_rows),
                "score_status": facts.get("score_status"),
                "ineligible_reasons": facts.get("ineligible_reasons") or [],
                "deterministic_facts": facts,
                "deterministic_rating": rating,
            }
        )

    summaries.sort(
        key=lambda item: (
            item.get("last_message_at") is not None,
            item.get("last_message_at") or datetime.min,
            str(item.get("cx_number") or ""),
        ),
        reverse=True,
    )
    return summaries


def build_whatsapp_thread_timeline(rows: list[dict[str, Any]], *, max_text_chars: int = 220) -> list[dict[str, Any]]:
    ordered_rows = sorted(rows, key=lambda row: row.get("timestamp") or datetime.min)
    timeline: list[dict[str, Any]] = []
    for index, row in enumerate(ordered_rows, start=1):
        speaker = normalize_conversation_speaker(row.get("direction"))
        timeline.append(
            {
                "idx": index,
                "time": row.get("timestamp"),
                "speaker": speaker,
                "direction": row.get("direction"),
                "text": _truncate_text(row.get("content"), max_chars=max_text_chars),
                "is_empty": not _normalized_text(row.get("content")),
            }
        )
    return timeline


def build_whatsapp_thread_llm_context(
    *,
    admin_number: str,
    cx_number: str,
    start_timestamp: datetime,
    end_timestamp: datetime | None,
    conversation_type: ConversationType,
    device: str,
    rows: list[dict[str, Any]],
    max_text_chars: int = 220,
) -> dict[str, Any]:
    facts = build_whatsapp_thread_facts(rows)
    rating = build_whatsapp_thread_rating(facts)
    warnings: list[str] = []
    if str(conversation_type or "all").strip().lower() != "all":
        warnings.append("direction_filtered_subset")

    return {
        "review_type": "sales_customer_conversation",
        "query": {
            "admin_number": admin_number,
            "cx_number": cx_number,
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
            "conversation_type": conversation_type,
            "device": device,
        },
        "staff": {"admin_number": admin_number},
        "customer": {"cx_number": cx_number},
        "deterministic_facts": facts,
        "deterministic_rating": rating,
        "warnings": warnings,
        "messages": build_whatsapp_thread_timeline(rows, max_text_chars=max_text_chars),
    }


def parse_whatsapp_thread_llm_review(review_text: Any) -> dict[str, Any]:
    raw_text = str(review_text or "").strip()
    if not raw_text:
        return {
            "parse_status": "error",
            "status": "parse_error",
            "reason": "LLM returned empty text.",
        }

    candidate = raw_text
    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, flags=re.I | re.S)
    if fenced_match:
        candidate = fenced_match.group(1).strip()
    else:
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start >= 0 and end > start:
            candidate = raw_text[start : end + 1]

    try:
        parsed = json.loads(candidate)
    except Exception as exc:
        return {
            "parse_status": "error",
            "status": "parse_error",
            "reason": f"LLM response was not valid JSON: {exc}",
        }

    if not isinstance(parsed, dict):
        return {
            "parse_status": "error",
            "status": "parse_error",
            "reason": "LLM response JSON was not an object.",
        }

    status = str(parsed.get("status") or "").strip().lower()
    if not status:
        status = "no_score" if parsed.get("quality_score") in (None, "") else "scored"
    parsed["status"] = status
    parsed["parse_status"] = "ok"
    return parsed


def build_whatsapp_thread_llm_prompt(llm_context: dict[str, Any]) -> str:
    return _LLM_REVIEW_TEMPLATE.replace(
        "{conversation_json}",
        json.dumps(llm_context, ensure_ascii=False, indent=2, default=str),
    )


__all__ = [
    "ConversationType",
    "build_whatsapp_thread_facts",
    "build_whatsapp_thread_llm_context",
    "build_whatsapp_thread_llm_prompt",
    "build_whatsapp_thread_rating",
    "build_whatsapp_thread_summaries",
    "build_whatsapp_thread_timeline",
    "get_whatsapp_conversations",
    "normalize_conversation_speaker",
    "parse_whatsapp_thread_llm_review",
]
