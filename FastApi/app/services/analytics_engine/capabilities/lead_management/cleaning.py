from __future__ import annotations

from typing import Any, Dict, List, Optional

from .common import (
    LLM_DROP_EVENT_FIELDS,
    compact_dict,
    order_event_fields,
)

def clean_counts_for_mode(counts: Dict[str, Any], mode: str = "raw") -> Dict[str, Any]:
    cleaned = dict(counts or {})
    if mode in {"llm", "evidence", "public"}:
        cleaned.pop("needs_review", None)
        # Let missing_context carry absent channels; remove zero counters to save tokens.
        for key in list(cleaned.keys()):
            if key != "events" and cleaned.get(key) == 0:
                cleaned.pop(key, None)
    return compact_dict(cleaned)


def clean_event_for_mode(event: Dict[str, Any], mode: str = "raw") -> Dict[str, Any]:
    cleaned = compact_dict(dict(event or {}))
    raw_count = cleaned.pop("raw_count", None)
    if raw_count not in (None, "", 1, "1"):
        cleaned["duplicate_count"] = raw_count

    if mode in {"llm", "evidence", "public"}:
        drop_fields = set(LLM_DROP_EVENT_FIELDS) | {
            "actor_role", "actor_source", "kind", "category", "priority",
            "sender", "receiver", "audio_url", "user_id", "lead_id", "source_id",
            "match_by", "direction", "admin_number", "needs_review",
        }
        for key in drop_fields:
            cleaned.pop(key, None)

        channel = str(cleaned.get("channel") or "").strip().lower()
        status = str(cleaned.get("status") or "").strip().lower()

        if channel == "call":
            cleaned.pop("text", None)
            if status == "missed" and int(cleaned.get("duration_sec") or 0) <= 0:
                cleaned.pop("duration_sec", None)

        if channel in {"travel_cart", "booking"}:
            if cleaned.get("property_id"):
                cleaned.pop("prop_id", None)
            # The compact text already carries dates/type/amount/source. Keep structured IDs only.
            if cleaned.get("text"):
                for key in ("travel_from_date", "travel_to_date", "nights", "booking_type", "total_amount", "advance_amount", "pending_amount", "cart_source"):
                    cleaned.pop(key, None)
            if cleaned.get("booking_status") == cleaned.get("status"):
                cleaned.pop("booking_status", None)

        if channel == "site_visit":
            cleaned.pop("text", None)

        if channel == "whatsapp" and status in {"read", "sent", "delivered"}:
            cleaned.pop("status", None)

    return order_event_fields(compact_dict(cleaned))


def clean_events_for_mode(events: List[Dict[str, Any]], mode: str = "raw", limit: Optional[int] = None) -> List[Dict[str, Any]]:
    rows = events[:limit] if limit else events
    return [clean_event_for_mode(row, mode=mode) for row in rows]


