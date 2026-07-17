#!/usr/bin/env python3
"""Compatibility wrapper for the lead communication review capability.

The implementation is split under `lead_management/`:
- common.py
- cleaning.py
- evidence.py
- summary.py
- prompt.py
- llm_client.py
- parsing.py
- cache.py
- dashboard.py
- rating_runner.py
- jobs.py

Keep importing this module from existing routes/CLI code; it re-exports the
public functions/classes used by older callers.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict
from app.services.analytics_engine.capabilities.lead_management.prompt import _lead_payload_for_llm
import app.services.analytics_engine.capabilities.lead_management.common as _lead_common

# -----------------------------------------------------------------------------
# Compatibility bootstrap for split lead_management modules.
# Some deployments have cleaning.py importing constants from lead_management.common
# that may be missing in older/common-lite copies. Inject only missing names before
# importing cleaning/evidence so the existing split modules can start cleanly.
# -----------------------------------------------------------------------------
_RAW_ONLY_EVENT_FIELDS = {
    "source_id",
    "match_by",
    "direction",
    "admin_number",
}
_LLM_DROP_EVENT_FIELDS = _RAW_ONLY_EVENT_FIELDS | {"lead_id", "needs_review"}
_EVENT_FIELD_ORDER = (
    "time", "channel", "flow", "status", "actor_role", "actor", "actor_name",
    "customer_number", "customer_email", "sender", "receiver", "kind", "category",
    "priority", "duration_sec", "text", "transcript", "duplicate_count",
    "user_id", "prop_id", "travel_from_date", "travel_to_date", "nights",
    "booking_type", "total_amount", "advance_amount", "pending_amount", "cart_source",
    "source_id", "lead_id", "direction", "admin_number", "needs_review", "match_by",
)
_CONTEXT_ONLY_TYPES = {
    "messagecontextinfo", "contextinfo", "protocolmessage", "senderkeydistributionmessage",
    "ephemeralmessage", "keepinchatmessage",
}
_MEDIA_TYPES = {
    "imagemessage": "image", "image": "image",
    "videomessage": "video", "video": "video",
    "audiomessage": "audio", "audio": "audio", "ptt": "audio", "voicemessage": "audio",
    "documentmessage": "document", "document": "document",
    "locationmessage": "location", "livelocationmessage": "location", "location": "location",
    "stickermessage": "sticker", "sticker": "sticker",
    "contactmessage": "contact", "contactsarraymessage": "contact", "contact": "contact",
}
_ACK_TEXT = {"ok", "okay", "k", "kk", "done", "noted", "thanks", "thank you", "yes", "ya", "yep", "👍", "👍👍"}
_KEYWORDS = {
    "payment": ("payment", "paid", "rent", "cash", "upi", "refund", "deposit", "amount", "balance", "receipt", "reference", "utr", "invoice"),
    "support": ("issue", "problem", "not working", "fan", "power", "electric", "water", "leak", "damage", "broken", "repair", "maintenance", "complaint", "ticket"),
    "booking": ("extension", "agreement", "kyc", "check in", "check-in", "checkout", "check out", "booking", "cancel", "cancellation", "app", "email", "visit", "occupant"),
    "urgency": ("urgent", "asap", "immediately", "today", "right now", "now", "tomorrow"),
}
_AUTOMATION_PATTERNS = [
    ("auto_payment_ack", re.compile(r"\b(thank you for updating your .*reference|we(?:'|’)?ve received your payment|we have received your payment|received your payment of|netbanking/upi reference)\b", re.I)),
    ("auto_welcome", re.compile(r"\b(welcome to rentmystay|thank you for choosing rentmystay|welcome to your new home|download our app)\b", re.I)),
    ("auto_agreement", re.compile(r"\b(rental agreement .*renewed|agreement .*renewed|complete your kyc|sign the agreement|smooth check-in)\b", re.I)),
    ("auto_ticket_update", re.compile(r"\b(your issue with ticket number|issue with ticket number .*resolved|representative has visited your flat for ticket)\b", re.I)),
    ("auto_booking", re.compile(r"\b(thanks for booking with rentmystay|booking id|bkid|this is a reminder that your check-in|check-in is at|check-out date)\b", re.I)),
    ("auto_site_visit", re.compile(r"\b(approved site visit|site visit .*confirmed|your site visit is confirmed|hurray! your site visit)\b", re.I)),
]
_ROLE_ALIASES = (
    ("caretaker", ("caretaker", "care taker")),
    ("sales", ("sales", "sale", "leasing", "inside sales", "field sales")),
    ("ops", ("ops", "operation", "operations", "property", "supervisor", "superviser", "manager")),
    ("finance", ("finance", "fin", "account", "accounts", "payment")),
    ("support", ("support", "service", "customer care", "helpdesk")),
    ("admin", ("admin", "administrator")),
    ("system", ("system", "automation", "bot", "noreply", "no-reply")),
)

for _name, _value in {
    "RAW_ONLY_EVENT_FIELDS": _RAW_ONLY_EVENT_FIELDS,
    "LLM_DROP_EVENT_FIELDS": _LLM_DROP_EVENT_FIELDS,
    "EVENT_FIELD_ORDER": _EVENT_FIELD_ORDER,
    "CONTEXT_ONLY_TYPES": _CONTEXT_ONLY_TYPES,
    "MEDIA_TYPES": _MEDIA_TYPES,
    "ACK_TEXT": _ACK_TEXT,
    "KEYWORDS": _KEYWORDS,
    "AUTOMATION_PATTERNS": _AUTOMATION_PATTERNS,
    "ROLE_ALIASES": _ROLE_ALIASES,
}.items():
    if not hasattr(_lead_common, _name):
        setattr(_lead_common, _name, _value)


def _clean_text_no_truncate(value: Any, max_len: int | None = None) -> str:
    if value in (None, ""):
        return ""
    out = re.sub(r"\s+", " ", str(value)).strip()
    if not max_len or max_len >= 100000:
        return out
    return out if len(out) <= max_len else out[: max_len - 3].rstrip() + "..."


def _call_transcript_text_transcript_text_only(row: Dict[str, Any], max_len: int) -> str:
    transcript = _clean_text_no_truncate(row.get("transcript_text"), max_len)
    return transcript or "no transcript"

# Use only staging_call_log_unified.transcript_text for call transcript evidence.
_lead_common.call_transcript_text = _call_transcript_text_transcript_text_only

from app.services.analytics_engine.capabilities.lead_management.common import *  # noqa: F401,F403
from app.services.analytics_engine.capabilities.lead_management.cleaning import *  # noqa: F401,F403
from app.services.analytics_engine.capabilities.lead_management.evidence import *  # noqa: F401,F403
from app.services.analytics_engine.capabilities.lead_management.summary import *  # noqa: F401,F403
from app.services.analytics_engine.capabilities.lead_management.prompt import *  # noqa: F401,F403
from app.services.analytics_engine.capabilities.lead_management.llm_client import *  # noqa: F401,F403
from app.services.analytics_engine.capabilities.lead_management.parsing import *  # noqa: F401,F403
from app.services.analytics_engine.capabilities.lead_management.cache import (
    LEAD_LLM_REVIEW_CACHE_TABLE,
    ensure_lead_review_cache_table,
)
from app.services.analytics_engine.capabilities.lead_management.dashboard import list_lead_dashboard_rows
from app.services.analytics_engine.capabilities.lead_management.jobs import (
    list_stale_lead_ids,
    mark_leads_stale,
    recompute_stale_leads,
)
from app.services.analytics_engine.capabilities.lead_management.rating_runner import LeadCommunicationReviewRunner

# Explicit private-name re-exports used by existing routes/runners.
from app.services.analytics_engine.capabilities.lead_management.evidence import (
    _add_unique,
    _append_unique_id,
    _collect_call_counterparty_phones,
    _dedupe_keep_order,
)
from app.services.analytics_engine.capabilities.lead_management.llm_client import (
    _extract_llm_text,
    _json_param,
    _stable_json_hash,
)
from app.services.analytics_engine.capabilities.lead_management.parsing import (
    _cell_by_header,
    _clean_table_cell,
    _fallback_priority_score,
    _header_key_map,
    _is_markdown_separator_cell,
    _max_priority_score,
    _score_number,
)
from app.services.analytics_engine.capabilities.lead_management.prompt import _lead_payload_for_llm

# ------------------------------------------------------------------------------------------------------------------

LEAD_REVIEW_CONTEXT_VERSION = "review_lead_communication:v11_raw_direction_trace"


def _v11_clean_value(value: Any) -> Any:
    if value in ("", [], {}, ()):
        return None
    return value


def _v11_compact_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in data.items() if _v11_clean_value(value) is not None}


def _v11_direction_from_flow(flow: Any) -> str:
    text = str(flow or "").strip().lower()

    if text in {"customer_to_sales", "customer_to_caretaker", "customer_to_ops", "customer_to_support"}:
        return "customer_inbound"

    if text in {"sales_to_customer", "caretaker_to_customer", "ops_to_customer", "support_to_customer"}:
        return "business_outbound"

    if text == "customer_activity":
        return "customer_activity"

    if text == "system_activity":
        return "system_activity"

    return "unknown"


def _v11_event_type(row: Dict[str, Any]) -> str:
    channel = str(row.get("channel") or "").strip().lower()
    flow = str(row.get("flow") or "").strip().lower()

    if channel in {"site_visit", "travel_cart", "booking"} or flow in {"customer_activity", "system_activity"}:
        return "activity"

    return "communication"


def _v11_role_from_event(row: Dict[str, Any]) -> str | None:
    role = row.get("actor_role") or row.get("role")
    if role not in (None, ""):
        return str(role).strip().lower()

    flow = str(row.get("flow") or "").strip().lower()
    if "sales" in flow:
        return "sales"
    if "caretaker" in flow:
        return "caretaker"
    if "ops" in flow:
        return "ops"
    if "support" in flow:
        return "support"
    if "finance" in flow:
        return "finance"
    return None

def _v11_parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None

    if isinstance(value, datetime):
        return value.replace(tzinfo=None)

    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())

    text_value = str(value).strip()
    if not text_value or text_value.lower() in {"none", "null", "nan"}:
        return None

    # Handle ISO values like 2026-04-18T16:10:20 or 2026-04-18 16:10:20.352000
    try:
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None)
    except Exception:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text_value[:26], fmt)
        except Exception:
            continue

    return None


def _v11_has_visible_booking(lead_payload: Dict[str, Any], rows: list[dict[str, Any]]) -> bool:
    if any(str(row.get("channel") or "").lower() == "booking" for row in rows):
        return True

    booking_ids = lead_payload.get("booking_ids") or lead_payload.get("booking_id")
    if booking_ids:
        return True

    status = str(lead_payload.get("status") or "").strip().lower()
    return status in {"booked", "converted", "success", "confirmed"}


def _v11_conversion_boundary(
    lead_payload: Dict[str, Any],
    rows: list[dict[str, Any]],
) -> tuple[datetime | None, str]:
    """
    Conversion boundary priority:
    1. Visible booking event time
    2. lead_payload.conversion_time
    3. lead_payload.closed_at
    4. booking/status visible but exact time missing
    5. not visible
    """

    booking_times: list[datetime] = []

    for row in rows:
        if str(row.get("channel") or "").strip().lower() != "booking":
            continue

        event_time = _v11_parse_dt(
            row.get("time")
            or row.get("booking_time")
            or row.get("created_at")
        )
        if event_time:
            booking_times.append(event_time)

    if booking_times:
        return min(booking_times), "booking_event"

    conversion_time = _v11_parse_dt(lead_payload.get("conversion_time"))
    if conversion_time:
        return conversion_time, "conversion_time"

    closed_at = _v11_parse_dt(lead_payload.get("closed_at"))
    if closed_at:
        return closed_at, "lead_closed_at"

    if _v11_has_visible_booking(lead_payload, rows):
        return None, "booking_visible_no_time"

    return None, "not_visible"


def _v11_phase_for_event(
    row: Dict[str, Any],
    conversion_at: datetime | None,
    *,
    handoff_hours: int = 48,
) -> str:
    """
    Phase rule:
    - No conversion time => keep existing row phase if present, else pre_booking
    - event_time <= conversion_at => pre_booking
    - conversion_at < event_time <= conversion_at + 48h => handoff_onboarding
    - event_time > conversion_at + 48h => post_booking
    """

    if conversion_at is None:
        return str(row.get("phase") or "pre_booking")

    event_at = _v11_parse_dt(
        row.get("time")
        or row.get("created_at")
        or row.get("visit_at")
    )

    if event_at is None:
        return "unphased"

    if event_at <= conversion_at:
        return "pre_booking"

    if event_at <= conversion_at + timedelta(hours=handoff_hours):
        return "handoff_onboarding"

    return "post_booking"


def _v11_annotate_event_phases(
    rows: list[dict[str, Any]],
    conversion_at: datetime | None,
    *,
    handoff_hours: int = 48,
) -> list[dict[str, Any]]:
    phased_rows: list[dict[str, Any]] = []

    for row in rows:
        item = dict(row or {})
        item["phase"] = _v11_phase_for_event(
            item,
            conversion_at,
            handoff_hours=handoff_hours,
        )
        phased_rows.append(item)

    return phased_rows


def _v11_count_phases(rows: list[dict[str, Any]]) -> Dict[str, int]:
    counts = {
        "pre_booking": 0,
        "handoff_onboarding": 0,
        "post_booking": 0,
        "unphased": 0,
    }

    for row in rows:
        phase = str(row.get("phase") or "unphased").strip() or "unphased"
        if phase not in counts:
            counts[phase] = 0
        counts[phase] += 1

    return counts


def _v11_build_scope(
    lead_payload: Dict[str, Any],
    rows: list[dict[str, Any]],
    conversion_at: datetime | None,
    conversion_source: str,
) -> Dict[str, Any]:
    has_travel_cart = any(row.get("channel") == "travel_cart" for row in rows)
    has_booking = any(row.get("channel") == "booking" for row in rows)

    missing_context: list[str] = []

    if not has_travel_cart:
        missing_context.append("travel_cart")

    if not has_booking:
        missing_context.append("booking_confirmation")

    if conversion_source == "not_visible":
        missing_context.append("conversion_time")
    elif conversion_source == "booking_visible_no_time":
        missing_context.append("exact_conversion_time")

    if conversion_at is not None:
        conversion_status = "visible"
    elif conversion_source == "booking_visible_no_time":
        conversion_status = "visible_no_time"
    else:
        conversion_status = "not_visible"

    return {
        "score_phase": "pre_booking",
        "conversion": conversion_status,
        "conversion_source": conversion_source,
        "conversion_time": conversion_at.isoformat(sep=" ") if conversion_at else None,
        "handoff_window": "48h_after_conversion",
        "working_hours": "10:00-20:30 Asia/Kolkata",
        "missing_context": missing_context,
    }

def _v11_phase_from_context(row: Dict[str, Any], scope: Dict[str, Any]) -> str:
    return str(row.get("phase") or scope.get("score_phase") or "pre_booking")


def _v11_build_timeline_event(row: Dict[str, Any], scope: Dict[str, Any]) -> Dict[str, Any]:
    channel = row.get("channel")
    flow = row.get("flow")
    direction = _v11_direction_from_flow(flow)
    event_type = _v11_event_type(row)
    actor_role = _v11_role_from_event(row)

    item: Dict[str, Any] = {
        "time": row.get("time"),
        "phase": _v11_phase_from_context(row, scope),
        "event_type": event_type,
        "channel": channel,
        "flow": flow,
        "direction": direction,
        "raw_direction": row.get("direction"),
        "status": row.get("status"),
        "actor": row.get("actor") or row.get("actor_name"),
        "actor_role": actor_role,
        "actor_source": row.get("actor_source"),
        "customer_number": row.get("customer_number"),
        "customer_email": row.get("customer_email"),
        "admin_number": row.get("admin_number"),
        "sender": row.get("sender"),
        "receiver": row.get("receiver"),
        "kind": row.get("kind"),
        "category": row.get("category"),
        "duration_sec": row.get("duration_sec"),
        "within_business_hours": row.get("within_business_hours"),
        # "after_hours": row.get("after_hours"),
        # "work_window": row.get("work_window"),
        "text": row.get("text"),
        "transcript": row.get("transcript"),
        "source_id": row.get("source_id"),
        "lead_id": row.get("lead_id"),
        "sla_basis": row.get("sla_basis"),
        "duplicate_count": row.get("duplicate_count"),
        "match_by": row.get("match_by"),
    }

    if event_type == "activity":
        item.update(
            {
                "created_at": row.get("created_at"),
                "property_id": row.get("property_id") or row.get("prop_id"),
                "building_id": row.get("building_id"),
                "unit_type": row.get("unit_type"),
                "visit_at": row.get("visit_at"),
                "visit_type": row.get("visit_type"),
                "user_id": row.get("user_id"),
                "booking_id": row.get("booking_id"),
                "travel_from_date": row.get("travel_from_date"),
                "travel_to_date": row.get("travel_to_date"),
                "nights": row.get("nights"),
                "booking_type": row.get("booking_type"),
                "total_amount": row.get("total_amount"),
                "advance_amount": row.get("advance_amount"),
                "pending_amount": row.get("pending_amount"),
                "cart_source": row.get("cart_source"),
            }
        )

    return _v11_compact_dict(item)


def _v11_count_calls(rows: list[dict[str, Any]]) -> Dict[str, Any]:
    call_rows = [row for row in rows if row.get("channel") == "call"]

    def is_connected(row: Dict[str, Any]) -> bool:
        return str(row.get("status") or "").lower() == "connected"

    def is_missed(row: Dict[str, Any]) -> bool:
        return str(row.get("status") or "").lower() == "missed"

    customer_inbound = [row for row in call_rows if _v11_direction_from_flow(row.get("flow")) == "customer_inbound"]
    business_outbound = [row for row in call_rows if _v11_direction_from_flow(row.get("flow")) == "business_outbound"]

    return {
        "total": len(call_rows),
        "connected": sum(1 for row in call_rows if is_connected(row)),
        "missed": sum(1 for row in call_rows if is_missed(row)),
        "connected_under_30_sec": sum(
            1
            for row in call_rows
            if is_connected(row)
            and row.get("duration_sec") not in (None, "")
            and int(float(row.get("duration_sec") or 0)) < 30
        ),
        "talk_time_sec": sum(
            int(float(row.get("duration_sec") or 0))
            for row in call_rows
            if row.get("duration_sec") not in (None, "")
        ),
        "customer_inbound": {
            "total": len(customer_inbound),
            "connected": sum(1 for row in customer_inbound if is_connected(row)),
            "missed": sum(1 for row in customer_inbound if is_missed(row)),
            "missed_business_hours": sum(1 for row in customer_inbound if is_missed(row) and bool(row.get("within_business_hours"))),
            "missed_after_hours": sum(1 for row in customer_inbound if is_missed(row) and bool(row.get("after_hours"))),
        },
        "business_outbound": {
            "total": len(business_outbound),
            "connected": sum(1 for row in business_outbound if is_connected(row)),
            "missed": sum(1 for row in business_outbound if is_missed(row)),
            "missed_business_hours": sum(1 for row in business_outbound if is_missed(row) and bool(row.get("within_business_hours"))),
            "missed_after_hours": sum(1 for row in business_outbound if is_missed(row) and bool(row.get("after_hours"))),
        },
    }


def _v11_build_stakeholders(rows: list[dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        role = _v11_role_from_event(row)
        if not role or role in {"customer", "system"}:
            continue

        bucket = out.setdefault(role, {"actors": [], "events": 0, "channels": {}})
        bucket["events"] += 1

        channel = row.get("channel")
        if channel:
            bucket["channels"][str(channel)] = int(bucket["channels"].get(str(channel), 0)) + 1

        actor = row.get("actor") or row.get("actor_name")
        if actor and actor not in bucket["actors"]:
            bucket["actors"].append(actor)

    return out


def _v11_build_data_limits(rows: list[dict[str, Any]], lead_payload: Dict[str, Any], scope: Dict[str, Any]) -> list[str]:
    limits: list[str] = []

    call_rows = [row for row in rows if row.get("channel") == "call"]
    if call_rows and all(str(row.get("transcript") or "").strip().lower() in {"", "no transcript", "null", "none"} for row in call_rows):
        limits.append("call transcripts missing")

    if not lead_payload.get("conversion_time") and scope.get("conversion") == "not_visible":
        limits.append("conversion not visible")

    if any(int(row.get("duplicate_count") or 0) > 1 for row in rows):
        limits.append("duplicate emails possible")

    if not any(row.get("channel") == "booking" for row in rows):
        limits.append("no visible booking confirmation")

    if not any(row.get("channel") == "travel_cart" for row in rows):
        limits.append("no visible travel cart")

    if not any(row.get("channel") == "whatsapp" for row in rows):
        limits.append("no WhatsApp messages visible")

    return limits

def build_lead_review_v11_payload(
    *,
    lead_id: int,
    window_label: str,
    start_dt: Any,
    end_dt: Any,
    schema_name: str,
    lead_payload: Dict[str, Any],
    rows: list[dict[str, Any]],
    limit: int | None = 200,
) -> Dict[str, Any]:
    conversion_at, conversion_source = _v11_conversion_boundary(
        lead_payload,
        rows,
    )

    phased_rows = _v11_annotate_event_phases(
        rows,
        conversion_at,
        handoff_hours=48,
    )

    visible_rows = phased_rows if not limit else phased_rows[: int(limit)]

    scope = _v11_build_scope(
        lead_payload,
        phased_rows,
        conversion_at,
        conversion_source,
    )

    timeline = [_v11_build_timeline_event(row, scope) for row in visible_rows]
    phase_counts = _v11_count_phases(phased_rows)

    lead_block = {
        **lead_payload,
        "conversion_time": lead_payload.get("conversion_time")
        or (conversion_at.isoformat(sep=" ") if conversion_at else None),
        "conversion_source": conversion_source,
    }

    return {
        "context_version": LEAD_REVIEW_CONTEXT_VERSION,
        "context": {
            "lead_id": lead_id,
            "window": window_label,
            "from": start_dt.isoformat(sep=" ") if hasattr(start_dt, "isoformat") else str(start_dt),
            "to": end_dt.isoformat(sep=" ") if hasattr(end_dt, "isoformat") else str(end_dt),
            "schema": schema_name,
            "timezone": "Asia/Kolkata",
        },
        "lead": lead_block,
        "scope": scope,
        "metrics": {
            "events": len(phased_rows),
            "calls": _v11_count_calls(phased_rows),
            "emails": {
                "total": sum(1 for row in phased_rows if row.get("channel") == "email"),
                "duplicates_possible": any(int(row.get("duplicate_count") or 0) > 1 for row in phased_rows),
            },
            "whatsapp": sum(1 for row in phased_rows if row.get("channel") == "whatsapp"),
            "site_visits": sum(1 for row in phased_rows if row.get("channel") == "site_visit"),
            "travel_cart": sum(1 for row in phased_rows if row.get("channel") == "travel_cart"),
            "booking_events": sum(1 for row in phased_rows if row.get("channel") == "booking"),

            # New phase metrics
            "pre_booking_events": phase_counts.get("pre_booking", 0),
            "handoff_onboarding_events": phase_counts.get("handoff_onboarding", 0),
            "post_booking_events": phase_counts.get("post_booking", 0),
            "unphased_events": phase_counts.get("unphased", 0),
            "phase_counts": phase_counts,
        },
        "stakeholders": _v11_build_stakeholders(phased_rows),
        "timeline": timeline,
        "data_limits": _v11_build_data_limits(phased_rows, lead_payload, scope),
    }

from app.services.analytics_engine.capabilities.lead_management.prompt import (
    build_lead_handling_prompt as _build_lead_handling_prompt_v2,
)

def build_lead_handling_prompt(lead_data: Dict[str, Any]) -> str:
    return _build_lead_handling_prompt_v2(lead_data)
# def build_lead_handling_prompt(lead_data: Dict[str, Any]) -> str:
#     from app.services.analytics_engine.capabilities.lead_management.prompt import (
#     build_lead_handling_prompt as _build_lead_handling_prompt_v2,
# )

# def build_lead_handling_prompt(lead_data: Dict[str, Any]) -> str:
#     return _build_lead_handling_prompt_v2(lead_data)

# ------------------------------------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Review communication for one lead_id only.")
    parser.add_argument("--lead-id", type=int, required=True)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--llm", action="store_true", help="Render LLM-ready evidence prompt.")
    parser.add_argument("--format", choices=["table", "llm", "json"], default="table")
    parser.add_argument("--hide-automation", action="store_true")
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--print-limit", type=int, default=200)
    parser.add_argument("--max-text", type=int, default=220)
    parser.add_argument("--output", default=None)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()

    payload = build_payload(args)
    render_format = "llm" if args.llm else args.format
    if render_format == "json":
        rendered = json.dumps(payload, default=str, ensure_ascii=False, indent=2)
    elif render_format == "llm":
        rendered = render_llm(payload, args.print_limit)
    else:
        rendered = render_table(payload, args.print_limit)

    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
