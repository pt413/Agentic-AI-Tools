from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .common import (
    WORK_WINDOW_LABEL,
    compact_dict,
    is_business_to_customer_flow,
    is_customer_to_business_flow,
    parse_event_dt,
)

def event_counts(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_flow: Dict[str, int] = {}
    for event in events:
        flow = str(event.get("flow") or "unknown").strip() or "unknown"
        by_flow[flow] = by_flow.get(flow, 0) + 1
    return compact_dict({
        "events": len(events),
        "calls": sum(1 for e in events if e.get("channel") == "call"),
        "whatsapp": sum(1 for e in events if e.get("channel") == "whatsapp"),
        "emails": sum(1 for e in events if e.get("channel") == "email"),
        "bookings": sum(1 for e in events if e.get("channel") == "booking"),
        "site_visits": sum(1 for e in events if e.get("channel") == "site_visit"),
        "travel_cart": sum(1 for e in events if e.get("channel") == "travel_cart"),
        "customer_to_business": sum(1 for e in events if is_customer_to_business_flow(e.get("flow"))),
        "business_to_customer": sum(1 for e in events if is_business_to_customer_flow(e.get("flow"))),
        "customer_activity": sum(1 for e in events if e.get("flow") == "customer_activity"),
        "by_flow": dict(sorted(by_flow.items())),
    })



def count_channels(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    return {k: v for k, v in sorted(Counter(str(row.get("channel") or "unknown") for row in rows).items()) if v}


def count_flows(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    return {k: v for k, v in sorted(Counter(str(row.get("flow") or "unknown") for row in rows).items()) if v}


def call_signal_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    calls = [row for row in rows if row.get("channel") == "call"]
    connected = [
        row for row in calls
        if str(row.get("status") or "").lower() == "connected" or int(row.get("duration_sec") or 0) > 0
    ]
    missed = [
        row for row in calls
        if str(row.get("status") or "").lower() == "missed" and int(row.get("duration_sec") or 0) <= 0
    ]
    customer_missed = [row for row in missed if is_customer_to_business_flow(row.get("flow"))]
    outbound_missed = [row for row in missed if is_business_to_customer_flow(row.get("flow"))]

    customer_missed_business_hours = [row for row in customer_missed if row.get("after_hours") is not True]
    customer_missed_after_hours = [row for row in customer_missed if row.get("after_hours") is True]
    outbound_missed_business_hours = [row for row in outbound_missed if row.get("after_hours") is not True]
    outbound_missed_after_hours = [row for row in outbound_missed if row.get("after_hours") is True]
    score_facing_missed = [row for row in missed if row.get("counts_against_sales_score") is not False]
    short_connected = [row for row in connected if 0 < int(row.get("duration_sec") or 0) < 30]

    first_after_hours_customer_missed_at = min(
        (parse_event_dt(row.get("time")) for row in customer_missed_after_hours if parse_event_dt(row.get("time"))),
        default=None,
    )
    recovery_after_after_hours_missed = False
    if first_after_hours_customer_missed_at is not None:
        for row in calls:
            row_at = parse_event_dt(row.get("time"))
            if row_at is None or row_at <= first_after_hours_customer_missed_at:
                continue
            if row.get("after_hours") is True:
                continue
            if int(row.get("duration_sec") or 0) > 0 or str(row.get("status") or "").lower() == "connected":
                recovery_after_after_hours_missed = True
                break

    out = {
        "work_window": WORK_WINDOW_LABEL,
        "total": len(calls),
        "connected": len(connected),
        "missed_total": len(missed),
        "missed_score_facing": len(score_facing_missed),
        "customer_inbound_missed_business_hours": len(customer_missed_business_hours),
        "customer_inbound_missed_after_hours": len(customer_missed_after_hours),
        "customer_inbound_missed_total": len(customer_missed),
        "business_outbound_missed_business_hours": len(outbound_missed_business_hours),
        "business_outbound_missed_after_hours": len(outbound_missed_after_hours),
        "business_outbound_missed_total": len(outbound_missed),
        "after_hours_recovered_in_next_work_window_visible": recovery_after_after_hours_missed if customer_missed_after_hours else None,
        "connected_under_30_sec": len(short_connected),
        "talk_time_sec": sum(int(row.get("duration_sec") or 0) for row in connected),
        "scoring_rule": "Use business-hours missed inbound calls for score; after-hours customer inbound is context-only unless recovery is missing after the next working window.",
    }
    # Keep score-facing zeros so small LLMs do not treat total after-hours misses as penalties.
    keep_zero = {
        "total",
        "missed_score_facing",
        "customer_inbound_missed_business_hours",
        "customer_inbound_missed_after_hours",
        "business_outbound_missed_business_hours",
    }
    return {k: v for k, v in out.items() if v not in (None, "") and (v != 0 or k in keep_zero)}

def phase_conversion_boundary(lead: Dict[str, Any], events: List[Dict[str, Any]]) -> Tuple[Optional[datetime], Optional[str]]:
    booking_times = [parse_event_dt(row.get("time")) for row in events if row.get("channel") == "booking"]
    booking_times = [value for value in booking_times if value is not None]
    if booking_times:
        return min(booking_times), "booking_event"
    closed_at = parse_event_dt(lead.get("closed_at"))
    if closed_at:
        return closed_at, "lead_closed_at"
    if lead.get("booking_ids") or str(lead.get("status") or "").strip().lower() in {"booked", "converted", "success"}:
        return None, "booking_visible_no_time"
    return None, None


def phase_name_for_event(row: Dict[str, Any], conversion_at: Optional[datetime], handoff_hours: int = 48) -> str:
    if conversion_at is None:
        return "pre_booking"
    event_at = parse_event_dt(row.get("time"))
    if event_at is None:
        return "unphased"
    if event_at <= conversion_at:
        return "pre_booking"
    if event_at <= conversion_at + timedelta(hours=handoff_hours):
        return "handoff_onboarding"
    return "post_booking"


def stakeholder_from_event(row: Dict[str, Any]) -> str:
    role = str(row.get("actor_role") or "").strip().lower()
    if role:
        return role
    flow = str(row.get("flow") or "").strip().lower()
    if flow.startswith("customer_to_"):
        return flow.replace("customer_to_", "", 1) or "business"
    if flow.endswith("_to_customer"):
        return flow.replace("_to_customer", "", 1) or "business"
    if flow == "system_to_customer":
        return "system"
    if row.get("channel") == "site_visit":
        return "sales"
    return "unknown"


def stakeholder_activity_summary(events: List[Dict[str, Any]], conversion_at: Optional[datetime]) -> Dict[str, Any]:
    summary: Dict[str, Dict[str, Any]] = {}
    for row in events:
        stakeholder = stakeholder_from_event(row)
        bucket = summary.setdefault(stakeholder, {
            "events": 0,
            "channels": Counter(),
            "phases": Counter(),
            "connected_calls": 0,
            "missed_calls_score_facing": 0,
            "missed_calls_after_hours_context": 0,
            "customer_inbound_after_hours": 0,
            "automation_events": 0,
            "actors": set(),
        })
        bucket["events"] += 1
        bucket["channels"][str(row.get("channel") or "unknown")] += 1
        bucket["phases"][phase_name_for_event(row, conversion_at)] += 1
        actor = row.get("actor") or row.get("actor_name")
        if actor:
            bucket["actors"].add(str(actor))
        if str(row.get("category") or "").startswith("auto_") or stakeholder == "system":
            bucket["automation_events"] += 1
        if row.get("channel") == "call":
            status = str(row.get("status") or "").lower()
            duration = int(row.get("duration_sec") or 0)
            if status == "connected" or duration > 0:
                bucket["connected_calls"] += 1
            elif status == "missed" and duration <= 0:
                if row.get("counts_against_sales_score") is False:
                    bucket["missed_calls_after_hours_context"] += 1
                else:
                    bucket["missed_calls_score_facing"] += 1
        if is_customer_to_business_flow(row.get("flow")) and row.get("after_hours") is True:
            bucket["customer_inbound_after_hours"] += 1

    out: Dict[str, Any] = {}
    for stakeholder, data in sorted(summary.items()):
        out[stakeholder] = compact_dict({
            "events": data["events"],
            "channels": dict(sorted(data["channels"].items())),
            "phases": dict(sorted(data["phases"].items())),
            "connected_calls": data["connected_calls"],
            "missed_calls_score_facing": data["missed_calls_score_facing"],
            "missed_calls_after_hours_context": data["missed_calls_after_hours_context"],
            "customer_inbound_after_hours": data["customer_inbound_after_hours"],
            "automation_events": data["automation_events"],
            "actors": sorted(data["actors"]),
        })
    return out


def build_lead_effectiveness_summary(lead: Dict[str, Any], events: List[Dict[str, Any]], counts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    counts = counts or event_counts(events)
    conversion_at, conversion_source = phase_conversion_boundary(lead, events)
    phase_rows: Dict[str, List[Dict[str, Any]]] = {"pre_booking": [], "handoff_onboarding": [], "post_booking": [], "unphased": []}
    for row in events:
        phase_rows.setdefault(phase_name_for_event(row, conversion_at), []).append(row)

    calls = [row for row in events if row.get("channel") == "call"]
    booking_rows = [row for row in events if row.get("channel") == "booking"]
    travel_rows = [row for row in events if row.get("channel") == "travel_cart"]
    site_rows = [row for row in events if row.get("channel") == "site_visit"]

    phase_counts = {
        "pre_booking": len(phase_rows.get("pre_booking") or []),
        "handoff_onboarding": len(phase_rows.get("handoff_onboarding") or []),
        "post_booking": len(phase_rows.get("post_booking") or []),
    }
    unphased_count = len(phase_rows.get("unphased") or [])
    if unphased_count:
        phase_counts["unphased"] = unphased_count

    missing: List[str] = []
    if counts.get("emails", 0) == 0:
        missing.append("emails")
    if counts.get("site_visits", 0) == 0:
        missing.append("site_visits")
    if counts.get("travel_cart", 0) == 0:
        missing.append("travel_cart")
    if calls and not any(row.get("transcript") for row in calls):
        missing.append("call_transcripts")
    if not (lead.get("booking_ids") or booking_rows):
        missing.append("booking_confirmation")
    if not conversion_at:
        missing.append("conversion_time")

    travel_summary = None
    if travel_rows:
        travel_summary = compact_dict({
            "count": len(travel_rows),
            "property_ids": sorted({str(row.get("property_id") or row.get("prop_id")) for row in travel_rows if row.get("property_id") or row.get("prop_id")}),
            "statuses": sorted({str(row.get("status")) for row in travel_rows if row.get("status") not in (None, "")}),
        })

    site_summary = None
    if site_rows:
        site_summary = compact_dict({
            "count": len(site_rows),
            "statuses": sorted({str(row.get("status")) for row in site_rows if row.get("status") not in (None, "")}),
        })

    return compact_dict({
        "conversion": compact_dict({
            "at": conversion_at.isoformat(timespec="seconds") if conversion_at else None,
            "source": conversion_source,
            "score_phase": "pre_booking",
            "handoff_window": "48h_after_conversion",
        }),
        "phase_counts": phase_counts,
        "working_hours": compact_dict({
            "timezone": "Asia/Kolkata",
            "window": WORK_WINDOW_LABEL,
            "score_rule": "After-hours customer inbound is context-only. Judge recovery from the next working window instead of penalizing immediate non-response.",
        }),
        "pre_booking_calls": call_signal_summary(phase_rows.get("pre_booking") or []),
        "stakeholders_involved": stakeholder_activity_summary(events, conversion_at),
        "post_booking": compact_dict({
            "events": len(phase_rows.get("post_booking") or []),
            "channels": count_channels(phase_rows.get("post_booking") or []),
            "flows": count_flows(phase_rows.get("post_booking") or []),
        }),
        "travel_cart": travel_summary,
        "site_visits": site_summary,
        "missing_context": missing,
    })


