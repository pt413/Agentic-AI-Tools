from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.analytics_engine.capabilities.lead_management.common import schema_ident

from .cache import (
    CARETAKER_PERFORMANCE_CACHE_TABLE,
    ensure_caretaker_performance_cache_table,
)

DEFAULT_SCHEMA = "AnalyticsEngine"
CARETAKER_DASHBOARD_WINDOW_DAYS = 30


def _json_value(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback

    if isinstance(value, (dict, list)):
        return value

    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return fallback

    return fallback


def _compact_dict(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    for key, item in (value or {}).items():
        if item in (None, "", [], {}, ()):
            continue

        if isinstance(item, dict):
            cleaned = _compact_dict(item)
            if cleaned:
                out[key] = cleaned

        elif isinstance(item, list):
            cleaned_list: list[Any] = []
            for child in item:
                if isinstance(child, dict):
                    cleaned_child = _compact_dict(child)
                    if cleaned_child:
                        cleaned_list.append(cleaned_child)
                elif child not in (None, "", [], {}, ()):
                    cleaned_list.append(child)

            if cleaned_list:
                out[key] = cleaned_list

        else:
            out[key] = item

    return out


def _num(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default

    try:
        return float(value)
    except Exception:
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_num(value, float(default))))
    except Exception:
        return default


def _round(value: Any, digits: int = 2) -> float | None:
    if value in (None, ""):
        return None

    try:
        return round(float(value), digits)
    except Exception:
        return None


def _first_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}, ()):
            return value
    return None


def _analytics_sections(metrics: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "communication": metrics.get("communication") if isinstance(metrics.get("communication"), dict) else {},
        "site_visits": metrics.get("site_visits") if isinstance(metrics.get("site_visits"), dict) else {},
        "tickets": metrics.get("tickets") if isinstance(metrics.get("tickets"), dict) else {},
        "property_management": metrics.get("property_management")
        if isinstance(metrics.get("property_management"), dict)
        else {},
        "workload": metrics.get("workload") if isinstance(metrics.get("workload"), dict) else {},
        "data_visibility": metrics.get("data_visibility") if isinstance(metrics.get("data_visibility"), dict) else {},
    }


def _site_visit_numbers(site_visits: dict[str, Any]) -> dict[str, Any]:
    total = _int(site_visits.get("total"))
    done = _int(site_visits.get("done_visits") or site_visits.get("actual_staff_visits"))
    scheduled_not_done = _int(
        site_visits.get("scheduled_not_done_visits")
        or site_visits.get("system_scheduled_visits")
    )

    booking_full = _int(site_visits.get("not_done_due_to_booking_full"))
    connected_followup = _int(site_visits.get("not_done_with_connected_followup"))
    missed_no_followup = _int(site_visits.get("not_done_missed_call_no_connected_followup"))
    missed_by_caretaker = _int(
    site_visits.get("missed_call_no_followup_visits_with_caretaker_attempt")
)
    missed_by_customer = _int(
    site_visits.get("missed_call_no_followup_visits_with_customer_attempt")
)
    missed_both_sides = _int(
        site_visits.get("missed_call_no_followup_visits_with_both_sides")
        or site_visits.get("missed_call_no_followup_both_sides")
    )
    missed_customer_only = _int(
        site_visits.get("missed_call_no_followup_visits_customer_only")
        or site_visits.get("missed_call_no_followup_customer_only")
    )
    missed_unknown_direction = _int(
        site_visits.get("missed_call_no_followup_visits_unknown_direction")
        or site_visits.get("missed_call_no_followup_unknown_direction")
    )
    no_booking_no_call = _int(site_visits.get("not_done_no_booking_no_call_activity"))
    unknown = _int(site_visits.get("not_done_unknown_reason"))
    pre_visit_call = (
        site_visits.get("pre_visit_call")
        if isinstance(site_visits.get("pre_visit_call"), dict)
        else {}
    )

    risky = missed_no_followup + no_booking_no_call
    explainable_or_recovered = booking_full + connected_followup

    return {
        "total": total,
        "done": done,
        "scheduled_not_done": scheduled_not_done,
        "booking_full": booking_full,
        "connected_followup": connected_followup,
        "missed_no_followup": missed_no_followup,
        "missed_call_source": {
            "caretaker": missed_by_caretaker,
            "customer": missed_by_customer,
            "both_sides": missed_both_sides,
            "customer_only": missed_customer_only,
            "unknown": missed_unknown_direction,
        },
        "no_booking_no_call": no_booking_no_call,
        "unknown": unknown,
        "risky": risky,
        "explainable_or_recovered": explainable_or_recovered,
        "pre_visit_call": {
            "done_site_visits": _int(pre_visit_call.get("done_site_visits") or done),
            "done_site_visits_with_pre_call": _int(pre_visit_call.get("done_site_visits_with_pre_call")),
            "done_site_visits_without_pre_call": _int(pre_visit_call.get("done_site_visits_without_pre_call")),
            "pre_call_coverage_pct": _round(pre_visit_call.get("pre_call_coverage_pct")),
            "avg_call_minutes_before_visit": _round(pre_visit_call.get("avg_call_minutes_before_visit")),
        },
        "done_rate_pct": _round(site_visits.get("done_visit_rate_pct") or site_visits.get("actual_visit_rate_pct")),
        "actual_to_scheduled_ratio_pct": _round(
            site_visits.get("done_to_scheduled_not_done_ratio_pct")
            or site_visits.get("actual_to_system_ratio_pct")
        ),
    }


def _site_visit_intelligence(site_visits: dict[str, Any]) -> dict[str, Any]:
    sv = _site_visit_numbers(site_visits)
    pre_visit_call = sv["pre_visit_call"]
    missed_call_source = sv["missed_call_source"]

    if sv["total"] <= 0:
        headline = "No site visit evidence visible"
        manager_story = "No site visit records were visible for this caretaker in the selected 30-day window."
        status = "no_data"
    elif sv["risky"] > 0:
        headline = f"{sv['risky']} risky not-done visits out of {sv['total']} total visits"
        manager_story = (
            f"Out of {sv['scheduled_not_done']} scheduled/not-done visits, "
            f"{sv['booking_full']} were explainable because the property was booked/full, "
            f"{sv['connected_followup']} had connected follow-up, and "
            f"{sv['risky']} need manager attention."
        )
        status = "needs_attention"
    else:
        headline = f"No risky not-done visits detected out of {sv['total']} total visits"
        manager_story = (
            f"{sv['scheduled_not_done']} visits were scheduled/not done, but no risky missed-follow-up "
            "or no-activity bucket was detected."
        )
        status = "healthy_or_explainable"

    buckets = [
        {
            "key": "done_visits",
            "label": "Done visits",
            "count": sv["done"],
            "severity": "good",
            "meaning": "Visit was completed.",
            "manager_interpretation": "Positive field execution evidence.",
        },
        {
            "key": "not_done_due_to_booking_full",
            "label": "Booking/full",
            "count": sv["booking_full"],
            "severity": "neutral",
            "meaning": "Property had overlapping successful booking/full evidence.",
            "manager_interpretation": "Usually explainable; treat as scheduling/availability context, not direct caretaker failure.",
        },
        {
            "key": "not_done_with_connected_followup",
            "label": "Connected follow-up",
            "count": sv["connected_followup"],
            "severity": "recovered",
            "meaning": "Visit was not completed, but connected call follow-up exists near the visit.",
            "manager_interpretation": "Recovered or partially handled. Review only if repeated.",
        },
        {
            "key": "not_done_missed_call_no_connected_followup",
            "label": "Missed-call/no connected follow-up",
            "count": sv["missed_no_followup"],
            "severity": "risk",
            "meaning": "Missed/zero-duration call near visit and no connected follow-up after last missed call.",
            "manager_interpretation": (
                f"{sv['missed_no_followup']} visits needed follow-up. "
                f"Customers called but got no caretaker response in {missed_call_source['customer_only']} visits. "
                f"The caretaker tried contacting customers in {missed_call_source['caretaker']} visits."
            ),
        },
        {
            "key": "not_done_no_booking_no_call_activity",
            "label": "No booking/no call activity",
            "count": sv["no_booking_no_call"],
            "severity": "high_risk",
            "meaning": "No booking/full explanation and no visible call activity near the scheduled visit.",
            "manager_interpretation": "Strong risk. Check whether visit happened offline or was ignored.",
        },
        {
            "key": "not_done_unknown_reason",
            "label": "Unknown reason",
            "count": sv["unknown"],
            "severity": "unknown",
            "meaning": "Fallback bucket when reason could not be classified.",
            "manager_interpretation": "Data-quality review needed before judging.",
        },
    ]

    return {
        "title": "Site Visit Intelligence",
        "status": status,
        "headline": headline,
        "manager_story": manager_story,
        "totals": {
            "total_site_visits": sv["total"],
            "done_visits": sv["done"],
            "scheduled_not_done_visits": sv["scheduled_not_done"],
            "done_visit_rate_pct": sv["done_rate_pct"],
            "done_to_scheduled_not_done_ratio_pct": sv["actual_to_scheduled_ratio_pct"],
            "pre_visit_call": pre_visit_call,
        },
        "risk_math": {
            "risky_site_visit_rows": sv["risky"],
            "risky_formula": (
                "not_done_missed_call_no_connected_followup "
                "+ not_done_no_booking_no_call_activity"
            ),
            "missed_call_no_connected_followup": sv["missed_no_followup"],
            "missed_call_no_followup_visits_with_caretaker_attempt": missed_call_source["caretaker"],
            "missed_call_no_followup_visits_with_customer_attempt": missed_call_source["customer"],
            "missed_call_no_followup_visits_with_both_sides": missed_call_source["both_sides"],
            "missed_call_no_followup_visits_customer_only": missed_call_source["customer_only"],
            "missed_call_no_followup_visits_unknown_direction": missed_call_source["unknown"],
            "missed_call_no_followup_from_caretaker": missed_call_source["caretaker"],
            "missed_call_no_followup_from_customer": missed_call_source["customer"],
            "missed_call_no_followup_both_sides": missed_call_source["both_sides"],
            "missed_call_no_followup_customer_only": missed_call_source["customer_only"],
            "missed_call_no_followup_unknown_direction": missed_call_source["unknown"],
            "no_booking_no_call_activity": sv["no_booking_no_call"],
            "explainable_or_recovered_not_done": sv["explainable_or_recovered"],
            "explainable_or_recovered_formula": (
                "not_done_due_to_booking_full "
                "+ not_done_with_connected_followup"
            ),
            "booking_full_explainable": sv["booking_full"],
            "connected_followup_recovered": sv["connected_followup"],
        },
        "buckets": buckets,
        "manager_note": (
            "Do not treat all scheduled/not-done visits as caretaker failure. "
            "Prioritize risky buckets first."
        ),
    }


def _communication_health(communication: dict[str, Any]) -> dict[str, Any]:
    total_calls = _int(communication.get("total_calls"))
    connected_calls = _int(communication.get("connected_calls"))
    missed_calls = _int(communication.get("missed_or_zero_duration_calls"))
    connect_rate = _round(communication.get("connect_rate_pct"))
    missed_rate = _round(communication.get("missed_rate_pct"))
    followup = communication.get("followup") if isinstance(communication.get("followup"), dict) else {}
    followup_required = _int(followup.get("missed_calls_requiring_followup"))
    followed_up = _int(followup.get("followed_up_count"))
    followup_rate = _round(followup.get("followup_rate_pct"))
    avg_followup_hours = _round(followup.get("avg_followup_hours"))

    if total_calls <= 0:
        status = "no_data"
        headline = "No call evidence visible"
    elif followup_required > 0 and avg_followup_hours is not None:
        status = "healthy" if followup_rate is not None and followup_rate >= 80 else "watch"
        headline = f"Avg follow-up {avg_followup_hours}h, {followup_rate}% recovery"
    elif missed_rate is not None and missed_rate >= 30:
        status = "needs_attention"
        headline = f"{missed_rate}% missed/zero-duration call rate"
    elif connect_rate is not None and connect_rate >= 75:
        status = "healthy"
        headline = f"{connect_rate}% connect rate"
    else:
        status = "watch"
        headline = f"{connect_rate}% connect rate, {missed_rate}% missed/zero-duration rate"

    return {
        "title": "Communication Health",
        "status": status,
        "headline": headline,
        "totals": {
            "total_calls": total_calls,
            "connected_calls": connected_calls,
            "missed_or_zero_duration_calls": missed_calls,
            "connect_rate_pct": connect_rate,
            "missed_rate_pct": missed_rate,
            "internal_calls": _int(communication.get("internal_calls")),
            "external_calls": _int(communication.get("external_calls")),
            "internal_call_ratio_pct": _round(communication.get("internal_call_ratio_pct")),
            "external_call_ratio_pct": _round(communication.get("external_call_ratio_pct")),
            "talk_time_sec": _int(communication.get("talk_time_sec")),
            "avg_connected_call_duration_sec": _round(communication.get("avg_connected_call_duration_sec")),
            "whatsapp_total": _int(communication.get("whatsapp_total")),
            "whatsapp_direct": _int(communication.get("whatsapp_direct")),
            "whatsapp_groups": _int(communication.get("whatsapp_groups")),
            "office_numbers_used": _int(communication.get("office_numbers_used")),
            "followup_required": followup_required,
            "followed_up_count": followed_up,
            "without_connected_followup_count": _int(followup.get("without_connected_followup_count")),
            "followup_rate_pct": followup_rate,
            "avg_followup_minutes": _round(followup.get("avg_followup_minutes")),
            "avg_followup_hours": avg_followup_hours,
            "median_followup_minutes": _round(followup.get("median_followup_minutes")),
        },
        "manager_note": (
            "Follow-up metrics are calculated from missed/zero-duration calls when the activity collector "
            "provides paired recovery data. Site-visit-specific follow-up risk is still measured separately."
        ),
    }


def _ticket_context(tickets: dict[str, Any]) -> dict[str, Any]:
    total = _int(tickets.get("assigned_building_tickets_total") or tickets.get("total"))
    closed = _int(tickets.get("assigned_building_tickets_closed_in_window") or tickets.get("closed"))
    reopened = _int(tickets.get("assigned_building_tickets_reopened") or tickets.get("reopened"))

    avg_rating = _round(
        tickets.get("assigned_building_avg_ticket_rating")
        or tickets.get("avg_ticket_rating")
    )

    rating_sum = _round(
        tickets.get("assigned_building_ticket_rating_sum")
        or tickets.get("ticket_rating_sum")
    )

    rating_count = _int(
        tickets.get("assigned_building_ticket_rating_count")
        or tickets.get("ticket_rating_count")
        or tickets.get("assigned_building_tickets_rated")
        or tickets.get("tickets_rated")
    )

    rated = _int(
        tickets.get("assigned_building_tickets_rated")
        or tickets.get("tickets_rated")
        or rating_count
    )

    unrated = _int(
        tickets.get("assigned_building_tickets_unrated")
        or tickets.get("tickets_unrated")
    )

    rating_coverage = _round(
        _first_value(
            tickets.get("assigned_building_ticket_rating_coverage_pct"),
            tickets.get("ticket_rating_coverage_pct"),
        )
    )

    resolution_count = _int(
        _first_value(
            tickets.get("assigned_building_ticket_resolution_count"),
            tickets.get("ticket_resolution_count"),
        )
    )

    avg_resolution_hours = _round(
        _first_value(
            tickets.get("assigned_building_avg_ticket_resolution_hours"),
            tickets.get("avg_ticket_resolution_hours"),
        )
    )

    median_resolution_hours = _round(
        _first_value(
            tickets.get("assigned_building_median_ticket_resolution_hours"),
            tickets.get("median_ticket_resolution_hours"),
        )
    )

    if total <= 0:
        status = "no_data"
        headline = "No assigned-building ticket context visible"
    elif avg_rating is not None and avg_rating < 3:
        status = "quality_concern"
        headline = f"{total} building-context tickets, avg rating {avg_rating} from {rated} rated"
    else:
        status = "visible"
        headline = f"{total} building-context tickets visible, {rated} rated"

    return {
        "title": "Assigned-Building Ticket Context",
        "status": status,
        "headline": headline,
        "totals": {
            "visible_tickets": total,
            "closed_tickets": closed,
            "open_tickets": _int(tickets.get("open")),
            "reopened_tickets": reopened,

            # Ticket rating formula fields
            "avg_ticket_rating": avg_rating,
            "rated_tickets": rated,
            "unrated_tickets": unrated,
            "ticket_rating_sum": rating_sum,
            "ticket_rating_count": rating_count,
            "ticket_rating_coverage_pct": rating_coverage,
            "rating_formula": (
                f"{rating_sum} / {rating_count} = {avg_rating}"
                if rating_sum is not None and rating_count > 0 and avg_rating is not None
                else None
            ),

            # Ticket resolution speed
            "ticket_resolution_count": resolution_count,
            "avg_ticket_resolution_hours": avg_resolution_hours,
            "median_ticket_resolution_hours": median_resolution_hours,
            "min_ticket_resolution_hours": _round(tickets.get("min_ticket_resolution_hours")),
            "max_ticket_resolution_hours": _round(tickets.get("max_ticket_resolution_hours")),
            "tickets_resolved_within_2h": _int(tickets.get("tickets_resolved_within_2h")),
            "tickets_resolved_2_to_24h": _int(tickets.get("tickets_resolved_2_to_24h")),
            "tickets_resolved_1_to_2d": _int(tickets.get("tickets_resolved_1_to_2d")),
            "tickets_resolved_after_2d": _int(tickets.get("tickets_resolved_after_2d")),
            "open_ticket_age_count": _int(tickets.get("open_ticket_age_count")),
            "avg_open_ticket_age_hours": _round(tickets.get("avg_open_ticket_age_hours")),

            "ticket_load_visible": bool(tickets.get("ticket_load_visible")),
        },
        "attribution_note": (
            "These are assigned-building/property-context ticket metrics. "
            "Do not interpret them as personally closed by the caretaker unless staff-owned ticket evidence is available."
        ),
    }

def _property_management_section(property_management: dict[str, Any]) -> dict[str, Any]:
    assigned_buildings = _int(property_management.get("assigned_buildings"))
    property_marks = _int(property_management.get("property_marks_by_staff"))

    checkin_received = _int(
        property_management.get("checkin_feedback_received")
        or property_management.get("checkin_feedback")
    )
    checkin_total = _int(property_management.get("checkin_feedback_total"))
    checkin_missing = _int(property_management.get("checkin_feedback_missing"))
    checkin_coverage = _round(property_management.get("checkin_feedback_coverage_pct"))

    checkout_received = _int(
        property_management.get("checkout_feedback_received")
        or property_management.get("checkout_feedback")
    )
    checkout_total = _int(property_management.get("checkout_feedback_total"))
    checkout_missing = _int(property_management.get("checkout_feedback_missing"))
    checkout_coverage = _round(property_management.get("checkout_feedback_coverage_pct"))

    if assigned_buildings <= 0:
        status = "no_assignment_visible"
        headline = "No assigned building visible"
    else:
        status = "visible"
        headline = f"{assigned_buildings} assigned buildings"

    return {
        "title": "Property Management",
        "status": status,
        "headline": headline,
        "totals": {
            "assigned_buildings": assigned_buildings,
            "vacant_properties": _int(property_management.get("vacant_properties")),
            "property_marks_by_staff": property_marks,

            # Backward-compatible received counts
            "checkin_feedback": checkin_received,
            "checkout_feedback": checkout_received,

            # Manager dashboard received / total fields
            "checkin_feedback_received": checkin_received,
            "checkin_feedback_total": checkin_total,
            "checkin_feedback_missing": checkin_missing,
            "checkin_feedback_coverage_pct": checkin_coverage,

            "checkout_feedback_received": checkout_received,
            "checkout_feedback_total": checkout_total,
            "checkout_feedback_missing": checkout_missing,
            "checkout_feedback_coverage_pct": checkout_coverage,

            "avg_checkin_stay_rating": _round(property_management.get("avg_checkin_stay_rating")),
            "avg_checkin_cleaning_rating": _round(property_management.get("avg_checkin_cleaning_rating")),
            "avg_checkout_rms_rating": _round(property_management.get("avg_checkout_rms_rating")),
            "avg_checkout_building_rating": _round(property_management.get("avg_checkout_building_rating")),
        },
        "manager_note": (
            "Check-in and checkout feedback totals are calculated from successful bookings "
            "for assigned properties in the selected 30-day window."
        ),
    }


def _workload_section(workload: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": "Workload",
        "totals": {
            "calls_per_day": _round(workload.get("calls_per_day")),
            "site_visits_per_day": _round(workload.get("site_visits_per_day")),
            "actual_site_visits_per_day": _round(workload.get("actual_site_visits_per_day")),
            "communication_events_per_day": _round(workload.get("communication_events_per_day")),
            "total_visible_activity_units": _int(workload.get("total_visible_activity_units")),
            "visible_activity_units_per_day": _round(workload.get("visible_activity_units_per_day")),
        },
    }


def _build_why_risky(
    *,
    main_reason: str | None,
    site_visit_section: dict[str, Any],
    communication_section: dict[str, Any],
    ticket_section: dict[str, Any],
) -> list[dict[str, Any]]:
    chips: list[dict[str, Any]] = []

    risk_math = site_visit_section.get("risk_math") or {}
    comm_totals = communication_section.get("totals") or {}
    ticket_totals = ticket_section.get("totals") or {}

    risky = _int(risk_math.get("risky_site_visit_rows"))
    missed_no_followup = _int(risk_math.get("missed_call_no_connected_followup"))
    no_booking_no_call = _int(risk_math.get("no_booking_no_call_activity"))

    if risky > 0:
        chips.append(
            {
                "severity": "high",
                "label": f"{risky} risky site visits",
                "detail": (
                    f"{missed_no_followup} missed-call/no connected follow-up + "
                    f"{no_booking_no_call} no booking/no call activity"
                ),
            }
        )

    missed_rate = _round(comm_totals.get("missed_rate_pct"))
    missed_calls = _int(comm_totals.get("missed_or_zero_duration_calls"))
    if missed_calls > 0 and missed_rate is not None:
        severity = "medium" if missed_rate < 30 else "high"
        chips.append(
            {
                "severity": severity,
                "label": f"{missed_rate}% missed/zero-duration call rate",
                "detail": f"{missed_calls} missed/zero-duration calls overall",
            }
        )

    avg_ticket_rating = _round(ticket_totals.get("avg_ticket_rating"))
    if avg_ticket_rating is not None and avg_ticket_rating < 3:
        chips.append(
            {
                "severity": "medium",
                "label": f"Low ticket rating {avg_ticket_rating}",
                "detail": "Assigned-building/property-context ticket rating, not necessarily personal attribution.",
            }
        )

    if main_reason:
        chips.insert(
            0,
            {
                "severity": "summary",
                "label": "Main reason",
                "detail": main_reason,
            },
        )

    return chips


def _deterministic_manager_actions(
    *,
    site_visit_section: dict[str, Any],
    communication_section: dict[str, Any],
    ticket_section: dict[str, Any],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []

    risk_math = site_visit_section.get("risk_math") or {}
    comm_totals = communication_section.get("totals") or {}
    ticket_totals = ticket_section.get("totals") or {}

    risky = _int(risk_math.get("risky_site_visit_rows"))
    missed_no_followup = _int(risk_math.get("missed_call_no_connected_followup"))
    no_booking_no_call = _int(risk_math.get("no_booking_no_call_activity"))
    booking_full = _int(risk_math.get("booking_full_explainable"))

    if risky > 0:
        actions.append(
            {
                "priority_score": 8,
                "owner_team": "Caretaker Team",
                "action": (
                    "Audit and recover risky not-done site visits: "
                    "missed-call/no-connected-follow-up and no-booking/no-call-activity."
                ),
                "evidence": (
                    f"{risky} risky not-done visits = "
                    f"{missed_no_followup} not_done_missed_call_no_connected_followup + "
                    f"{no_booking_no_call} not_done_no_booking_no_call_activity."
                ),
            }
        )

    if booking_full > 0:
        actions.append(
            {
                "priority_score": 6,
                "owner_team": "Operations/Scheduling",
                "action": (
                    "Improve scheduling/availability visibility so booked/full properties "
                    "are not repeatedly scheduled for visits."
                ),
                "evidence": f"{booking_full} not_done_due_to_booking_full.",
            }
        )

    missed_calls = _int(comm_totals.get("missed_or_zero_duration_calls"))
    missed_rate = _round(comm_totals.get("missed_rate_pct"))
    if missed_calls > 0 and missed_rate is not None and missed_rate >= 25:
        actions.append(
            {
                "priority_score": 5,
                "owner_team": "Communication Team",
                "action": "Improve missed-call response process and reduce zero-duration calls.",
                "evidence": (
                    f"{missed_calls} missed_or_zero_duration_calls; "
                    f"missed_rate_pct = {missed_rate}%."
                ),
            }
        )

    avg_ticket_rating = _round(ticket_totals.get("avg_ticket_rating"))
    visible_tickets = _int(ticket_totals.get("visible_tickets"))
    reopened = _int(ticket_totals.get("reopened_tickets"))
    if visible_tickets > 0 and avg_ticket_rating is not None and avg_ticket_rating < 3:
        actions.append(
            {
                "priority_score": 5,
                "owner_team": "Ops / Ticket Owners",
                "action": (
                    "Review low assigned-building ticket rating and separate staff-owned tickets "
                    "from building-context tickets."
                ),
                "evidence": (
                    f"{visible_tickets} assigned-building/property-context tickets; "
                    f"avg_ticket_rating = {avg_ticket_rating}; reopened_tickets = {reopened}."
                ),
            }
        )

    return actions


def _main_table_view(
    *,
    item: dict[str, Any],
    staff: dict[str, Any],
    rating: dict[str, Any],
    site_visit_section: dict[str, Any],
    communication_section: dict[str, Any],
    ticket_section: dict[str, Any],
    property_section: dict[str, Any],
    manager_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    sv_totals = site_visit_section.get("totals") or {}
    sv_risk = site_visit_section.get("risk_math") or {}
    comm = communication_section.get("totals") or {}
    tickets = ticket_section.get("totals") or {}
    props = property_section.get("totals") or {}

    risky = _int(sv_risk.get("risky_site_visit_rows"))
    booking_full = _int(sv_risk.get("booking_full_explainable"))
    pre_visit_call = sv_totals.get("pre_visit_call") if isinstance(sv_totals.get("pre_visit_call"), dict) else {}
    pre_call_done = _int(pre_visit_call.get("done_site_visits_with_pre_call"))
    pre_call_total = _int(pre_visit_call.get("done_site_visits") or sv_totals.get("done_visits"))
    top_action = manager_actions[0].get("action") if manager_actions else None
    followup_required = _int(comm.get("followup_required"))
    avg_followup_hours = _round(comm.get("avg_followup_hours"))
    followup_rate = _round(comm.get("followup_rate_pct"))
    if followup_required > 0:
        communication_label = (
            f"follow-up avg {avg_followup_hours}h, {followup_rate}% recovery"
            if avg_followup_hours is not None
            else f"{followup_rate}% follow-up recovery"
        )
    else:
        communication_label = (
            f"{_round(comm.get('connect_rate_pct'))}% connect, "
            f"{_round(comm.get('missed_rate_pct'))}% missed/zero-duration"
        )

    return {
        "caretaker": staff.get("username"),
        "risk": rating.get("overall_risk"),
        "score": rating.get("overall_score"),
        "priority": rating.get("priority_score"),
        "why_risky": rating.get("main_reason"),
        "site_visit_health": (
            f"{_int(sv_totals.get('done_visits'))}/{_int(sv_totals.get('total_site_visits'))} done, "
            f"{risky} risky, {booking_full} booking/full, "
            f"{pre_call_done}/{pre_call_total} pre-call"
        ),
        "communication": communication_label,
        "ticket_quality": (
            f"{_int(tickets.get('visible_tickets'))} building-context tickets, "
            f"{_int(tickets.get('rated_tickets'))} rated, "
            f"avg rating {_round(tickets.get('avg_ticket_rating'))}"
        ),
        "buildings": _int(props.get("assigned_buildings")),
        "top_action": top_action,
        "rated_at": item.get("updated_at"),
    }


def _summary_cards(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total_caretakers = len(rows)
    high_risk = 0
    medium_risk = 0
    low_risk = 0

    total_site_visits = 0
    total_risky_visits = 0
    total_booking_full = 0
    total_connected_followup = 0
    total_missed_no_followup = 0
    total_no_booking_no_call = 0

    scores: list[float] = []

    for row in rows:
        risk = str((row.get("rating") or {}).get("overall_risk") or "").strip().lower()
        if risk == "high":
            high_risk += 1
        elif risk == "medium":
            medium_risk += 1
        elif risk == "low":
            low_risk += 1

        score = _num((row.get("rating") or {}).get("overall_score"), default=-1)
        if score >= 0:
            scores.append(score)

        site = ((row.get("sections") or {}).get("site_visit_intelligence") or {})
        totals = site.get("totals") or {}
        risk_math = site.get("risk_math") or {}

        total_site_visits += _int(totals.get("total_site_visits"))
        total_risky_visits += _int(risk_math.get("risky_site_visit_rows"))
        total_booking_full += _int(risk_math.get("booking_full_explainable"))
        total_connected_followup += _int(risk_math.get("connected_followup_recovered"))
        total_missed_no_followup += _int(risk_math.get("missed_call_no_connected_followup"))
        total_no_booking_no_call += _int(risk_math.get("no_booking_no_call_activity"))

    avg_score = round(sum(scores) / len(scores), 2) if scores else None

    return [
        {
            "key": "total_caretakers",
            "label": "Caretakers rated",
            "value": total_caretakers,
            "tone": "neutral",
        },
        {
            "key": "high_risk_caretakers",
            "label": "High risk",
            "value": high_risk,
            "tone": "danger",
        },
        {
            "key": "medium_risk_caretakers",
            "label": "Medium risk",
            "value": medium_risk,
            "tone": "warning",
        },
        {
            "key": "average_score",
            "label": "Average score",
            "value": avg_score,
            "tone": "neutral",
        },
        {
            "key": "total_site_visits",
            "label": "Total site visits",
            "value": total_site_visits,
            "tone": "neutral",
        },
        {
            "key": "risky_site_visits",
            "label": "Risky not-done visits",
            "value": total_risky_visits,
            "tone": "danger" if total_risky_visits else "good",
            "help_text": "missed-call/no-connected-follow-up + no-booking/no-call-activity",
        },
        {
            "key": "booking_full_explainable",
            "label": "Booking/full explainable",
            "value": total_booking_full,
            "tone": "neutral",
            "help_text": "Not direct caretaker failure; usually scheduling/availability context.",
        },
        {
            "key": "connected_followup_recovered",
            "label": "Connected follow-up recovered",
            "value": total_connected_followup,
            "tone": "good",
        },
        {
            "key": "missed_call_no_followup",
            "label": "Missed-call/no follow-up",
            "value": total_missed_no_followup,
            "tone": "danger",
        },
        {
            "key": "no_booking_no_call",
            "label": "No booking/no call",
            "value": total_no_booking_no_call,
            "tone": "danger",
        },
    ]


def list_caretaker_performance_dashboard_rows(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    username: str | None = None,
    phone: str | None = None,
    risk: str | None = None,
    status: str | None = None,
    max_days: int | None = None,
    min_score: float | None = None,
    max_score: float | None = None,
    min_priority: float | None = None,
    max_priority: float | None = None,
    search: str | None = None,
    sort_by: str = "activity",
    sort_dir: str = "desc",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """
    Manager-friendly caretaker performance dashboard reader.

    Business rule:
    - Dashboard is fixed to one 30-day rating row per caretaker.
    - Dashboard reads cache only.
    - Dashboard does not call LLM.
    - Dashboard shows active/inactive caretakers if they have a valid cached
      30-day rating.
    - Older custom-window rows such as 7-day or 90-day ratings are ignored.
    - Stale/error/not_rated rows are ignored in the business dashboard.
    """
    ensure_caretaker_performance_cache_table(db, schema)

    table = f"{schema_ident(schema)}.{CARETAKER_PERFORMANCE_CACHE_TABLE}"

    where_parts: list[str] = [
        "window_days = :window_days",
        "LOWER(COALESCE(status, '')) = 'ok'",
        "stale_at IS NULL",
    ]

    params: dict[str, Any] = {
        "window_days": CARETAKER_DASHBOARD_WINDOW_DAYS,
        "limit_n": max(1, min(int(limit or 100), 1000)),
        "offset_n": max(0, int(offset or 0)),
    }

    if username:
        where_parts.append("LOWER(COALESCE(username, '')) LIKE :username")
        params["username"] = f"%{username.strip().lower()}%"

    if phone:
        where_parts.append("COALESCE(phone, '') LIKE :phone")
        params["phone"] = f"%{phone.strip()}%"

    if risk:
        where_parts.append("LOWER(COALESCE(overall_risk, '')) = :risk")
        params["risk"] = risk.strip().lower()

    if min_score is not None:
        where_parts.append("overall_score >= :min_score")
        params["min_score"] = float(min_score)

    if max_score is not None:
        where_parts.append("overall_score <= :max_score")
        params["max_score"] = float(max_score)

    if min_priority is not None:
        where_parts.append("priority_score >= :min_priority")
        params["min_priority"] = float(min_priority)

    if max_priority is not None:
        where_parts.append("priority_score <= :max_priority")
        params["max_priority"] = float(max_priority)

    if search:
        where_parts.append(
            "("
            "COALESCE(username, '') ILIKE :search OR "
            "COALESCE(email, '') ILIKE :search OR "
            "COALESCE(phone, '') ILIKE :search OR "
            "COALESCE(main_reason, '') ILIKE :search OR "
            "COALESCE(overall_risk, '') ILIKE :search OR "
            "COALESCE(review_text, '') ILIKE :search OR "
            "COALESCE(staff_team, '') ILIKE :search OR "
            "COALESCE(role_scope, '') ILIKE :search"
            ")"
        )
        params["search"] = f"%{search.strip()}%"

    where_sql = "WHERE " + " AND ".join(where_parts)

    sort_map = {
        "activity": (
            "COALESCE("
            "NULLIF(metrics->'workload'->>'total_visible_activity_units', '')::numeric, "
            "0)"
        ),
        "score": "overall_score",
        "overall_score": "overall_score",
        "priority": "priority_score",
        "priority_score": "priority_score",
        "risk": "overall_risk",
        "updated_at": "updated_at",
        "rated_at": "updated_at",
        "tickets": "(metrics->'tickets'->>'total')::int",
        "tickets_open": "(metrics->'tickets'->>'open')::int",
        "tickets_closed": "(metrics->'tickets'->>'closed')::int",
        "calls": "(metrics->'communication'->>'total_calls')::int",
        "site_visits": "(metrics->'site_visits'->>'total')::int",
        "actual_visits": "(metrics->'site_visits'->>'actual_staff_visits')::int",
        "missed_calls": "(metrics->'communication'->>'missed_or_zero_duration_calls')::int",
        "connect_rate": "(metrics->'communication'->>'connect_rate_pct')::numeric",
        "actual_visit_rate": "(metrics->'site_visits'->>'actual_visit_rate_pct')::numeric",
        "risky_site_visits": (
            "COALESCE((metrics->'site_visits'->>'not_done_missed_call_no_connected_followup')::int, 0) "
            "+ COALESCE((metrics->'site_visits'->>'not_done_no_booking_no_call_activity')::int, 0)"
        ),
        "booking_full": "(metrics->'site_visits'->>'not_done_due_to_booking_full')::int",
        "window": "window_days",
        "days": "window_days",
    }

    sort_expr = sort_map.get(sort_by or "activity", sort_map["activity"])
    direction = "ASC" if str(sort_dir or "asc").lower() == "asc" else "DESC"

    rows = db.execute(
        text(
            f"""
            SELECT
                *,
                COUNT(*) OVER() AS _total_count
            FROM {table}
            {where_sql}
            ORDER BY {sort_expr} {direction} NULLS LAST, updated_at DESC NULLS LAST
            LIMIT :limit_n OFFSET :offset_n
            """
        ),
        params,
    ).mappings().fetchall()

    total = int(rows[0].get("_total_count") or 0) if rows else 0
    out_rows: list[dict[str, Any]] = []

    for row in rows:
        item = dict(row)

        summary = _json_value(item.get("summary"), {})
        metrics = _json_value(item.get("metrics"), {})
        context_tables = metrics.get("context_tables") if isinstance(metrics.get("context_tables"), dict) else {}
        action_rows = _json_value(item.get("action_rows"), [])

        sections = _analytics_sections(metrics)
        communication = sections["communication"]
        site_visits = sections["site_visits"]
        tickets = sections["tickets"]
        property_management = sections["property_management"]
        workload = sections["workload"]
        data_visibility = sections["data_visibility"]

        staff = {
            "username": item.get("username"),
            "email": item.get("email"),
            "phone": item.get("phone"),
            "team": item.get("staff_team"),
            "role_scope": item.get("role_scope"),
        }

        window = {
            "days": item.get("window_days"),
            "start": item.get("window_start"),
            "end": item.get("window_end"),
            "fixed_business_window": True,
        }

        rating = {
            "status": item.get("status"),
            "overall_score": item.get("overall_score"),
            "priority_score": item.get("priority_score"),
            "communication_score": item.get("communication_score"),
            "site_visit_score": item.get("site_visit_score"),
            "ticket_score": item.get("ticket_score"),
            "management_score": item.get("management_score"),
            "overall_risk": item.get("overall_risk"),
            "main_reason": item.get("main_reason"),
            "updated_at": item.get("updated_at"),
            "stale_at": item.get("stale_at"),
            "needs_recompute": False,
        }

        site_visit_section = _site_visit_intelligence(site_visits)
        communication_section = _communication_health(communication)
        ticket_section = _ticket_context(tickets)
        property_section = _property_management_section(property_management)
        workload_info = _workload_section(workload)

        deterministic_actions = _deterministic_manager_actions(
            site_visit_section=site_visit_section,
            communication_section=communication_section,
            ticket_section=ticket_section,
        )

        parsed_llm_actions = action_rows if isinstance(action_rows, list) else []

        why_risky = _build_why_risky(
            main_reason=item.get("main_reason"),
            site_visit_section=site_visit_section,
            communication_section=communication_section,
            ticket_section=ticket_section,
        )

        manager_actions = deterministic_actions or parsed_llm_actions

        identifier_params: dict[str, Any] = {}

        if item.get("username"):
            identifier_params["username"] = item.get("username")
        elif item.get("email"):
            identifier_params["email"] = item.get("email")
        elif item.get("phone"):
            identifier_params["phone"] = item.get("phone")

        detail_params = {
            "endpoint": "/analytics/capabilities/staff/caretaker-performance/llm-rating",
            **identifier_params,
            "days": CARETAKER_DASHBOARD_WINDOW_DAYS,
            "run_llm": "false",
            "use_cache": "true",
            "force_refresh": "false",
            "include_activity": "true",
            "include_prompt": "false",
        }

        detail_url = "/analytics/capabilities/ui?" + urlencode(detail_params)

        main_table = _main_table_view(
            item=item,
            staff=staff,
            rating=rating,
            site_visit_section=site_visit_section,
            communication_section=communication_section,
            ticket_section=ticket_section,
            property_section=property_section,
            manager_actions=manager_actions,
        )

        def _metric_first(*values: Any) -> Any:
            for value in values:
                if value not in (None, "", [], {}, ()):
                    return value
            return None

        ticket_rating_sum = _metric_first(
            tickets.get("ticket_rating_sum"),
            tickets.get("assigned_building_ticket_rating_sum"),
        )

        ticket_rating_count = _metric_first(
            tickets.get("ticket_rating_count"),
            tickets.get("assigned_building_ticket_rating_count"),
            tickets.get("tickets_rated"),
            tickets.get("assigned_building_tickets_rated"),
        )

        counts = {
            "assigned_buildings": property_management.get("assigned_buildings"),
            "vacant_properties": property_management.get("vacant_properties"),

            "visit_records": site_visits.get("total"),
            "actual_visits": _metric_first(
                site_visits.get("actual_staff_visits"),
                site_visits.get("done_visits"),
            ),
            "system_scheduled_visits": _metric_first(
                site_visits.get("system_scheduled_visits"),
                site_visits.get("scheduled_not_done_visits"),
            ),
            "risky_site_visits": (site_visit_section.get("risk_math") or {}).get("risky_site_visit_rows"),
            "booking_full_visits": (site_visit_section.get("risk_math") or {}).get("booking_full_explainable"),
            "connected_followup_visits": (site_visit_section.get("risk_math") or {}).get("connected_followup_recovered"),
            "missed_call_no_followup_visits": (site_visit_section.get("risk_math") or {}).get("missed_call_no_connected_followup"),
            "no_booking_no_call_visits": (site_visit_section.get("risk_math") or {}).get("no_booking_no_call_activity"),
            "done_site_visits_with_pre_call": (
                ((site_visit_section.get("totals") or {}).get("pre_visit_call") or {})
                .get("done_site_visits_with_pre_call")
            ),
            "done_site_visits_without_pre_call": (
                ((site_visit_section.get("totals") or {}).get("pre_visit_call") or {})
                .get("done_site_visits_without_pre_call")
            ),

            "calls": communication.get("total_calls"),
            "calls_connected": communication.get("connected_calls"),
            "calls_missed_or_zero_duration": communication.get("missed_or_zero_duration_calls"),
            "missed_calls_requiring_followup": (communication_section.get("totals") or {}).get("followup_required"),
            "followed_up_count": (communication_section.get("totals") or {}).get("followed_up_count"),
            "followup_rate_pct": (communication_section.get("totals") or {}).get("followup_rate_pct"),
            "avg_followup_hours": (communication_section.get("totals") or {}).get("avg_followup_hours"),
            "avg_followup_minutes": (communication_section.get("totals") or {}).get("avg_followup_minutes"),

            # Ticket context
            "tickets_total": _metric_first(
                tickets.get("total"),
                tickets.get("assigned_building_tickets_total"),
            ),
            "tickets_open": tickets.get("open"),
            "tickets_closed": tickets.get("closed"),
            "tickets_reopened": _metric_first(
                tickets.get("reopened"),
                tickets.get("assigned_building_tickets_reopened"),
            ),
            "tickets_closed_in_window": _metric_first(
                tickets.get("closed_in_window"),
                tickets.get("assigned_building_tickets_closed_in_window"),
            ),

            # Ticket rating logic:
            # avg_ticket_rating = ticket_rating_sum / ticket_rating_count
            "avg_ticket_rating": _metric_first(
                tickets.get("avg_ticket_rating"),
                tickets.get("assigned_building_avg_ticket_rating"),
            ),
            "ticket_rating_sum": ticket_rating_sum,
            "ticket_rating_count": ticket_rating_count,
            "tickets_rated": _metric_first(
                tickets.get("tickets_rated"),
                tickets.get("assigned_building_tickets_rated"),
                ticket_rating_count,
            ),
            "tickets_unrated": _metric_first(
                tickets.get("tickets_unrated"),
                tickets.get("assigned_building_tickets_unrated"),
            ),
            "ticket_rating_coverage_pct": _metric_first(
                tickets.get("ticket_rating_coverage_pct"),
                tickets.get("assigned_building_ticket_rating_coverage_pct"),
            ),

            # Ticket resolution speed
            "ticket_resolution_count": _metric_first(
                tickets.get("ticket_resolution_count"),
                tickets.get("assigned_building_ticket_resolution_count"),
            ),
            "avg_ticket_resolution_hours": _metric_first(
                tickets.get("avg_ticket_resolution_hours"),
                tickets.get("assigned_building_avg_ticket_resolution_hours"),
            ),
            "median_ticket_resolution_hours": _metric_first(
                tickets.get("median_ticket_resolution_hours"),
                tickets.get("assigned_building_median_ticket_resolution_hours"),
            ),
            "min_ticket_resolution_hours": tickets.get("min_ticket_resolution_hours"),
            "max_ticket_resolution_hours": tickets.get("max_ticket_resolution_hours"),
            "tickets_resolved_within_2h": tickets.get("tickets_resolved_within_2h"),
            "tickets_resolved_2_to_24h": tickets.get("tickets_resolved_2_to_24h"),
            "tickets_resolved_1_to_2d": tickets.get("tickets_resolved_1_to_2d"),
            "tickets_resolved_after_2d": tickets.get("tickets_resolved_after_2d"),
            "open_ticket_age_count": tickets.get("open_ticket_age_count"),
            "avg_open_ticket_age_hours": tickets.get("avg_open_ticket_age_hours"),

            "property_marks_by_staff": property_management.get("property_marks_by_staff"),

            # Feedback denominator logic:
            # received = feedback forms submitted
            # total = successful eligible check-in/check-out customers/bookings
            "checkin_feedback_received": _metric_first(
                property_management.get("checkin_feedback_received"),
                property_management.get("checkin_feedback"),
            ),
            "checkin_feedback_total": property_management.get("checkin_feedback_total"),
            "checkin_feedback_missing": property_management.get("checkin_feedback_missing"),
            "checkin_feedback_coverage_pct": property_management.get("checkin_feedback_coverage_pct"),

            "checkout_feedback_received": _metric_first(
                property_management.get("checkout_feedback_received"),
                property_management.get("checkout_feedback"),
            ),
            "checkout_feedback_total": property_management.get("checkout_feedback_total"),
            "checkout_feedback_missing": property_management.get("checkout_feedback_missing"),
            "checkout_feedback_coverage_pct": property_management.get("checkout_feedback_coverage_pct"),
        }

        derived = {
            "connect_rate_pct": communication.get("connect_rate_pct"),
            "missed_rate_pct": communication.get("missed_rate_pct"),
            "followup_rate_pct": (communication_section.get("totals") or {}).get("followup_rate_pct"),
            "avg_followup_hours": (communication_section.get("totals") or {}).get("avg_followup_hours"),
            "actual_visit_rate_pct": site_visits.get("actual_visit_rate_pct")
            or site_visits.get("done_visit_rate_pct"),
            "actual_to_system_ratio_pct": site_visits.get("actual_to_system_ratio_pct")
            or site_visits.get("done_to_scheduled_not_done_ratio_pct"),
            "pre_visit_call_coverage_pct": (
                ((site_visit_section.get("totals") or {}).get("pre_visit_call") or {})
                .get("pre_call_coverage_pct")
            ),
            "avg_pre_visit_call_minutes_before_visit": (
                ((site_visit_section.get("totals") or {}).get("pre_visit_call") or {})
                .get("avg_call_minutes_before_visit")
            ),
            "calls_per_day": workload.get("calls_per_day"),
            "visible_activity_units_per_day": workload.get("visible_activity_units_per_day"),
        }

        row_payload = {
            "cache_key": item.get("cache_key"),
            "staff": staff,
            "window": window,
            "rating": rating,
            "main_table": main_table,
            "why_risky": why_risky,
            "sections": {
                "site_visit_intelligence": site_visit_section,
                "communication_health": communication_section,
                "ticket_context": ticket_section,
                "property_management": property_section,
                "workload": workload_info,
                "manager_actions": manager_actions,
                "llm_actions": parsed_llm_actions,
            },
            "counts": counts,
            "derived": derived,
            "visibility": data_visibility,
            "actions": manager_actions,
            "action_count": len(manager_actions),
            "summary": summary,
            "metrics": metrics,
            "context_tables": {
            "risky_site_visits": context_tables.get("risky_site_visits") or [],
            "recent_site_visits": context_tables.get("recent_site_visits") or [],
},
            "detail_url": detail_url,
        }

        out_rows.append(_compact_dict(row_payload))

    return {
        "view": "caretaker_performance_dashboard",
        "display_mode": "manager_friendly_modular",
        "source_table": CARETAKER_PERFORMANCE_CACHE_TABLE,
        "business_window_days": CARETAKER_DASHBOARD_WINDOW_DAYS,
        "cache_policy": {
            "source": "cache_only",
            "llm_on_dashboard_load": False,
            "included_status": "ok",
            "exclude_stale": True,
            "shows_active_and_inactive_if_cached": True,
        },
        "summary_cards": _summary_cards(out_rows),
        "main_table_columns": [
            {"key": "caretaker", "label": "Caretaker"},
            {"key": "risk", "label": "Risk"},
            {"key": "score", "label": "Score"},
            {"key": "priority", "label": "Priority"},
            {"key": "why_risky", "label": "Why risky"},
            {"key": "site_visit_health", "label": "Site Visit Health"},
            {"key": "communication", "label": "Communication"},
            {"key": "ticket_quality", "label": "Ticket Context"},
            {"key": "buildings", "label": "Buildings"},
            {"key": "top_action", "label": "Top Action"},
            {"key": "rated_at", "label": "Rated At"},
        ],
        "detail_sections": [
            "overall_verdict",
            "site_visit_intelligence",
            "communication_health",
            "ticket_context",
            "property_management",
            "workload",
            "manager_actions",
        ],
        "limit": params["limit_n"],
        "offset": params["offset_n"],
        "total": total,
        "count": len(out_rows),
        "sort": {"by": sort_by, "dir": direction.lower()},
        "filters": {
            "username": username,
            "phone": phone,
            "risk": risk,
            "status": "ok",
            "requested_status_ignored": status,
            "window_days": CARETAKER_DASHBOARD_WINDOW_DAYS,
            "requested_max_days_ignored": max_days,
            "min_score": min_score,
            "max_score": max_score,
            "min_priority": min_priority,
            "max_priority": max_priority,
            "search": search,
        },
        "rows": out_rows,
    }
