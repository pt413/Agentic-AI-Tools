from __future__ import annotations

from datetime import datetime
from typing import Any


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _int(value: Any) -> int:
    return int(_num(value))


def _round(value: Any, digits: int = 2) -> float | None:
    if value in (None, ""):
        return None

    try:
        return round(float(value), digits)
    except Exception:
        return None


def _pct(part: Any, total: Any) -> float | None:
    total_n = _num(total)
    if total_n <= 0:
        return None
    return round((_num(part) / total_n) * 100, 2)


def _first_metric(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value not in (None, "", [], {}, ()):
            return value
    return None


def _compact(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if item not in (None, "", [], {}, ())
    }


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:26] if "." in text else text[:19], fmt)
        except Exception:
            continue
    return None


def _days_between(start: Any, end: Any, fallback: int = 7) -> int:
    start_dt = _parse_dt(start)
    end_dt = _parse_dt(end)
    if not start_dt or not end_dt:
        return fallback
    days = max(1, (end_dt - start_dt).days or 1)
    return days


def _top_call_counterparties(activity: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
    rows = activity.get("call_summary_by_counterparty") or []
    if not isinstance(rows, list):
        return []

    cleaned: list[dict[str, Any]] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        cleaned.append(
            _compact(
                {
                    "counterparty": row.get("counterparty"),
                    "type": row.get("type"),
                    "calls": row.get("calls"),
                    "connected": row.get("connected"),
                    "missed": row.get("missed"),
                    "talk_time_sec": row.get("talk_time_sec"),
                    "lead_ids": row.get("lead_ids"),
                    "last_time": row.get("last_time"),
                }
            )
        )
    return cleaned

def _site_visit_rows(activity: dict[str, Any]) -> list[dict[str, Any]]:
    data = activity.get("data") if isinstance(activity.get("data"), dict) else {}
    rows = activity.get("site_visits") or data.get("site_visits") or []
    return rows if isinstance(rows, list) else []


def _risky_site_visits(activity: dict[str, Any], limit: int = 50) -> list[dict[str, Any]]:
    counts = activity.get("counts") if isinstance(activity.get("counts"), dict) else {}

    prebuilt = (
        counts.get("risky_site_visit_rows")
        or activity.get("risky_site_visit_rows")
        or activity.get("site_visit_risky_rows")
        or activity.get("risky_site_visits")
        or []
    )

    if isinstance(prebuilt, list) and prebuilt:
        return [
            _compact(row)
            for row in prebuilt[:limit]
            if isinstance(row, dict)
        ]

    rows = _site_visit_rows(activity)

    risky_reasons = {
        "NOT_DONE_MISSED_CALL_NO_CONNECTED_FOLLOWUP",
        "NOT_DONE_NO_BOOKING_NO_CALL_ACTIVITY",
    }

    cleaned: list[dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        reason = str(row.get("not_done_reason_candidate") or row.get("reason") or "").strip()

        try:
            status_code = int(row.get("status_code") or row.get("schedule_status") or 0)
        except Exception:
            status_code = None

        if reason not in risky_reasons:
            continue

        cleaned.append(
            _compact(
                {
                    "time": row.get("time")
                    or row.get("site_visit_date")
                    or row.get("added_on")
                    or row.get("activity_time"),
                    "source_id": row.get("source_id"),
                    "lead_id": row.get("lead_id"),
                    "prop_id": row.get("prop_id"),
                    "property": row.get("property") or row.get("unit_name") or row.get("property_name"),
                    "building_id": row.get("building_id"),
                    "executive": row.get("executive") or row.get("executive_name"),
                    "activity_type": row.get("activity_type"),
                    "visit_type": row.get("visit_type") or row.get("type"),
                    "status": row.get("status"),
                    "status_code": status_code,
                    "not_done_reason_candidate": reason,
                    "calls_near_visit": row.get("calls_near_visit"),
                    "missed_calls_near_visit": row.get("missed_calls_near_visit"),
                    "missed_calls_from_caretaker_near_visit": row.get("missed_calls_from_caretaker_near_visit"),
                    "missed_calls_from_customer_near_visit": row.get("missed_calls_from_customer_near_visit"),
                    "missed_calls_unknown_direction_near_visit": row.get("missed_calls_unknown_direction_near_visit"),
                    "manager_reason": (
                        "Missed/zero-duration call found but no connected follow-up "
                        f"(caretaker missed/attempted: {row.get('missed_calls_from_caretaker_near_visit') or 0}, "
                        f"customer missed/attempted: {row.get('missed_calls_from_customer_near_visit') or 0})"
                        if reason == "NOT_DONE_MISSED_CALL_NO_CONNECTED_FOLLOWUP"
                        else "No booking/full explanation and no visible call activity"
                    ),
                }
            )
        )

        if len(cleaned) >= limit:
            break

    return cleaned


def _recent_site_visits(activity: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    rows = _site_visit_rows(activity)

    cleaned: list[dict[str, Any]] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        cleaned.append(
            _compact(
                {
                    "time": row.get("time") or row.get("site_visit_date") or row.get("added_on"),
                    "lead_id": row.get("lead_id"),
                    "prop_id": row.get("prop_id"),
                    "property": row.get("property") or row.get("unit_name") or row.get("property_name"),
                    "executive": row.get("executive") or row.get("executive_name"),
                    "activity_type": row.get("activity_type"),
                    "type": row.get("type") or row.get("visit_type"),
                    "status": row.get("status"),
                    "status_code": row.get("status_code") or row.get("schedule_status"),
                }
            )
        )
    return cleaned


def build_caretaker_analytics(activity: dict[str, Any]) -> dict[str, Any]:
    counts = activity.get("counts") if isinstance(activity.get("counts"), dict) else {}
    staff = activity.get("staff") if isinstance(activity.get("staff"), dict) else {}
    window = activity.get("window") if isinstance(activity.get("window"), dict) else {}
    input_data = activity.get("input") if isinstance(activity.get("input"), dict) else {}

    days = _int(input_data.get("days")) or _days_between(
        window.get("start"),
        window.get("end"),
        fallback=7,
    )

    total_calls = _int(counts.get("calls"))
    connected_calls = _int(counts.get("calls_connected"))
    missed_calls = _int(counts.get("calls_missed_or_zero_duration"))
    internal_calls = _int(counts.get("calls_internal"))
    external_calls = _int(counts.get("calls_external"))
    talk_time_sec = _int(counts.get("call_talk_time_sec"))

    missed_calls_followed_up = _int(
        _first_metric(
            counts,
            "missed_calls_followed_up",
            "missed_with_connected_followup_24h",
            "missed_with_connected_followup",
            "calls_missed_with_connected_followup",
        )
    )
    missed_calls_without_followup = _int(
        _first_metric(
            counts,
            "missed_calls_without_followup",
            "missed_without_connected_followup_24h",
            "missed_without_connected_followup",
            "calls_missed_without_connected_followup",
        )
    )
    missed_calls_requiring_followup = _int(
        _first_metric(
            counts,
            "missed_calls_requiring_followup",
            "missed_followup_required",
            "missed_calls_for_followup",
        )
    )
    if missed_calls_requiring_followup <= 0 and (missed_calls_followed_up or missed_calls_without_followup):
        missed_calls_requiring_followup = missed_calls_followed_up + missed_calls_without_followup

    avg_followup_minutes = _round(
        _first_metric(
            counts,
            "avg_followup_minutes",
            "avg_missed_call_followup_minutes",
            "avg_followup_delay_minutes",
            "avg_followup_time_minutes",
        )
    )
    avg_followup_hours = _round(
        _first_metric(
            counts,
            "avg_followup_hours",
            "avg_missed_call_followup_hours",
            "avg_followup_delay_hours",
            "avg_followup_time_hours",
        )
    )
    if avg_followup_hours is None and avg_followup_minutes is not None:
        avg_followup_hours = round(avg_followup_minutes / 60, 2)

    median_followup_minutes = _round(
        _first_metric(
            counts,
            "median_followup_minutes",
            "median_missed_call_followup_minutes",
            "median_followup_delay_minutes",
        )
    )
    followup_rate_pct = _round(
        _first_metric(
            counts,
            "followup_rate_pct",
            "followup_recovery_rate_pct",
            "missed_call_followup_rate_pct",
        )
    )
    if followup_rate_pct is None:
        followup_rate_pct = _pct(missed_calls_followed_up, missed_calls_requiring_followup)
    has_followup_metrics = any(
        value not in (None, "", 0)
        for value in (
            missed_calls_requiring_followup,
            missed_calls_followed_up,
            missed_calls_without_followup,
            avg_followup_minutes,
            avg_followup_hours,
            median_followup_minutes,
        )
    )

    whatsapp_total = _int(counts.get("whatsapp"))
    whatsapp_direct = _int(counts.get("whatsapp_direct"))
    whatsapp_groups = _int(counts.get("whatsapp_groups"))

    site_visits_total = _int(counts.get("site_visits"))
    site_visits_actual = _int(counts.get("site_visits_actual"))
    site_visits_system = _int(counts.get("site_visits_system"))
    site_visits_not_done_due_to_booking_full = _int(counts.get("site_visits_not_done_due_to_booking_full"))
    site_visits_not_done_with_connected_followup = _int(counts.get("site_visits_not_done_with_connected_followup"))
    site_visits_not_done_missed_call_no_connected_followup = _int(
        counts.get("site_visits_not_done_missed_call_no_connected_followup")
    )
    site_visits_missed_call_no_followup_from_caretaker = _int(
        counts.get("site_visits_missed_call_no_followup_from_caretaker")
    )
    site_visits_missed_call_no_followup_from_customer = _int(
        counts.get("site_visits_missed_call_no_followup_from_customer")
    )
    site_visits_missed_call_no_followup_both_sides = _int(
        counts.get("site_visits_missed_call_no_followup_both_sides")
    )
    site_visits_missed_call_no_followup_customer_only = _int(
        counts.get("site_visits_missed_call_no_followup_customer_only")
    )
    site_visits_missed_call_no_followup_unknown_direction = _int(
        counts.get("site_visits_missed_call_no_followup_unknown_direction")
    )
    site_visits_not_done_no_booking_no_call_activity = _int(
        counts.get("site_visits_not_done_no_booking_no_call_activity")
    )
    site_visits_not_done_unknown_reason = _int(counts.get("site_visits_not_done_unknown_reason"))
    done_site_visits_with_pre_call = _int(counts.get("done_site_visits_with_pre_call"))
    done_site_visits_without_pre_call = _int(counts.get("done_site_visits_without_pre_call"))
    pre_visit_call_coverage_pct = _round(counts.get("pre_visit_call_coverage_pct"))
    avg_pre_visit_call_minutes_before_visit = _round(
        counts.get("avg_pre_visit_call_minutes_before_visit")
    )
    tickets_total = _int(counts.get("tickets_total"))
    tickets_open = _int(counts.get("tickets_open"))
    tickets_closed = _int(counts.get("tickets_closed"))
    tickets_reopened = _int(counts.get("tickets_reopened"))

    tickets_rated = _int(counts.get("tickets_rated") or counts.get("ticket_rating_count"))
    tickets_unrated = _int(counts.get("tickets_unrated"))
    ticket_rating_sum = _round(counts.get("ticket_rating_sum"))
    ticket_rating_count = _int(counts.get("ticket_rating_count") or counts.get("tickets_rated"))
    ticket_rating_coverage_pct = _round(counts.get("ticket_rating_coverage_pct"))
    avg_ticket_rating = _round(counts.get("avg_ticket_rating"))

    # Ticket resolution speed from staging_user_ticket.created_at -> close_date.
    ticket_resolution_count = _int(counts.get("ticket_resolution_count"))
    avg_ticket_resolution_hours = _round(counts.get("avg_ticket_resolution_hours"))
    median_ticket_resolution_hours = _round(counts.get("median_ticket_resolution_hours"))
    min_ticket_resolution_hours = _round(counts.get("min_ticket_resolution_hours"))
    max_ticket_resolution_hours = _round(counts.get("max_ticket_resolution_hours"))
    tickets_resolved_within_2h = _int(counts.get("tickets_resolved_within_2h"))
    tickets_resolved_2_to_24h = _int(counts.get("tickets_resolved_2_to_24h"))
    tickets_resolved_1_to_2d = _int(counts.get("tickets_resolved_1_to_2d"))
    tickets_resolved_after_2d = _int(counts.get("tickets_resolved_after_2d"))
    open_ticket_age_count = _int(counts.get("open_ticket_age_count"))
    avg_open_ticket_age_hours = _round(counts.get("avg_open_ticket_age_hours"))

    assigned_buildings = _int(counts.get("assigned_buildings"))
    vacant_properties = _int(counts.get("vacant_properties"))

    avg_checkin_stay_rating = counts.get("avg_checkin_stay_rating")
    avg_checkin_cleaning_rating = counts.get("avg_checkin_cleaning_rating")
    avg_checkout_rms_rating = counts.get("avg_checkout_rms_rating")
    avg_checkout_building_rating = counts.get("avg_checkout_building_rating")

    checkin_feedback_received = _int(counts.get("checkin_feedback_received") or counts.get("checkin_feedback"))
    checkin_feedback_total = _int(counts.get("checkin_feedback_total"))
    checkin_feedback_missing = _int(counts.get("checkin_feedback_missing"))
    checkin_feedback_coverage_pct = _round(counts.get("checkin_feedback_coverage_pct"))

    checkout_feedback_received = _int(counts.get("checkout_feedback_received") or counts.get("checkout_feedback"))
    checkout_feedback_total = _int(counts.get("checkout_feedback_total"))
    checkout_feedback_missing = _int(counts.get("checkout_feedback_missing"))
    checkout_feedback_coverage_pct = _round(counts.get("checkout_feedback_coverage_pct"))

    return {
        "caretaker": _compact(
            {
                "username": staff.get("username"),
                "email": staff.get("email"),
                "team": staff.get("team"),
                "role": staff.get("role"),
                "account_type": staff.get("account_type"),
                "active": staff.get("active"),
                "role_scope": activity.get("role_scope") or input_data.get("resolved_role_scope"),
                "role_resolution_source": input_data.get("role_resolution_source"),
            }
        ),
        "window": _compact(
            {
                "days": days,
                "start": window.get("start"),
                "end": window.get("end"),
                "label": window.get("label"),
            }
        ),
        "communication": _compact(
            {
                "total_calls": total_calls,
                "connected_calls": connected_calls,
                "missed_or_zero_duration_calls": missed_calls,
                "connect_rate_pct": _pct(connected_calls, total_calls),
                "missed_rate_pct": _pct(missed_calls, total_calls),
                "internal_calls": internal_calls,
                "external_calls": external_calls,
                "internal_call_ratio_pct": _pct(internal_calls, total_calls),
                "external_call_ratio_pct": _pct(external_calls, total_calls),
                "talk_time_sec": talk_time_sec,
                "avg_connected_call_duration_sec": round(talk_time_sec / connected_calls, 2) if connected_calls else None,
                "whatsapp_total": whatsapp_total,
                "whatsapp_direct": whatsapp_direct,
                "whatsapp_groups": whatsapp_groups,
                "office_numbers_used": _int(counts.get("office_numbers_used")),
                "followup": _compact(
                    {
                        "missed_calls_requiring_followup": missed_calls_requiring_followup,
                        "followed_up_count": missed_calls_followed_up,
                        "without_connected_followup_count": missed_calls_without_followup,
                        "followup_rate_pct": followup_rate_pct,
                        "avg_followup_minutes": avg_followup_minutes,
                        "avg_followup_hours": avg_followup_hours,
                        "median_followup_minutes": median_followup_minutes,
                        "definition": (
                            "Missed/zero-duration calls recovered by a later connected follow-up call "
                            "when the staff activity collector provides paired follow-up metrics."
                        ),
                    }
                )
                if has_followup_metrics
                else None,
            }
        ),
        "site_visits": _compact(
            {
                "total": site_visits_total,

                # Correct visit status counts
                "done_visits": site_visits_actual,
                "scheduled_not_done_visits": site_visits_system,
                "done_visit_rate_pct": _pct(site_visits_actual, site_visits_total),
                "done_to_scheduled_not_done_ratio_pct": _pct(site_visits_actual, site_visits_system),
                "pre_visit_call": _compact(
                    {
                        "done_site_visits": site_visits_actual,
                        "done_site_visits_with_pre_call": done_site_visits_with_pre_call,
                        "done_site_visits_without_pre_call": done_site_visits_without_pre_call,
                        "pre_call_coverage_pct": pre_visit_call_coverage_pct,
                        "avg_call_minutes_before_visit": avg_pre_visit_call_minutes_before_visit,
                        "definition": (
                            "For done site visits, count a connected caretaker call to the same lead/customer "
                            "on the same date before the site visit time. Average uses the nearest previous "
                            "connected call per visit."
                        ),
                    }
                ),

                # Not-done reason buckets: neutral / explainable
                "not_done_due_to_booking_full": site_visits_not_done_due_to_booking_full,
                "not_done_with_connected_followup": site_visits_not_done_with_connected_followup,

                # Not-done reason buckets: negative / rating-impacting
                "not_done_missed_call_no_connected_followup": site_visits_not_done_missed_call_no_connected_followup,
                "missed_call_no_followup_visits_with_caretaker_attempt": site_visits_missed_call_no_followup_from_caretaker,
                "missed_call_no_followup_visits_with_customer_attempt": site_visits_missed_call_no_followup_from_customer,
                "missed_call_no_followup_visits_with_both_sides": site_visits_missed_call_no_followup_both_sides,
                "missed_call_no_followup_visits_customer_only": site_visits_missed_call_no_followup_customer_only,
                "missed_call_no_followup_visits_unknown_direction": site_visits_missed_call_no_followup_unknown_direction,

                # Backward-compatible keys. These are visit counts, not raw missed-call event counts.
                "missed_call_no_followup_from_caretaker": site_visits_missed_call_no_followup_from_caretaker,
                "missed_call_no_followup_from_customer": site_visits_missed_call_no_followup_from_customer,
                "missed_call_no_followup_both_sides": site_visits_missed_call_no_followup_both_sides,
                "missed_call_no_followup_customer_only": site_visits_missed_call_no_followup_customer_only,
                "missed_call_no_followup_unknown_direction": site_visits_missed_call_no_followup_unknown_direction,
                "not_done_no_booking_no_call_activity": site_visits_not_done_no_booking_no_call_activity,

                # Data-quality fallback only
                "not_done_unknown_reason": site_visits_not_done_unknown_reason,

                # Backward-compatible old keys for dashboard/API code that still reads them.
                # Meaning is now:
                # actual_staff_visits = done visits
                # system_scheduled_visits = scheduled/not-done visits
                "actual_staff_visits": site_visits_actual,
                "system_scheduled_visits": site_visits_system,
                "actual_visit_rate_pct": _pct(site_visits_actual, site_visits_total),
                "actual_to_system_ratio_pct": _pct(site_visits_actual, site_visits_system),
            }
        ),
        "tickets": _compact(
            {
                "total": tickets_total,
                "open": tickets_open,
                "closed": tickets_closed,
                "closed_in_window": _int(counts.get("tickets_closed_in_window")),
                "ticket_load_visible": tickets_total > 0,
                "no_ticket_environment": tickets_total == 0,

                # Assigned-building/property-context ticket metrics
                "assigned_building_tickets_closed_in_window": _int(counts.get("tickets_closed_in_window")),
                "assigned_building_tickets_total": tickets_total,
                "assigned_building_tickets_reopened": tickets_reopened,

                # Rating formula:
                # avg_ticket_rating = ticket_rating_sum / ticket_rating_count
                "assigned_building_avg_ticket_rating": avg_ticket_rating,
                "assigned_building_ticket_rating_sum": ticket_rating_sum,
                "assigned_building_ticket_rating_count": ticket_rating_count,
                "assigned_building_tickets_rated": tickets_rated,
                "assigned_building_tickets_unrated": tickets_unrated,
                "assigned_building_ticket_rating_coverage_pct": ticket_rating_coverage_pct,

                # Ticket resolution speed
                "ticket_resolution_count": ticket_resolution_count,
                "avg_ticket_resolution_hours": avg_ticket_resolution_hours,
                "median_ticket_resolution_hours": median_ticket_resolution_hours,
                "min_ticket_resolution_hours": min_ticket_resolution_hours,
                "max_ticket_resolution_hours": max_ticket_resolution_hours,
                "tickets_resolved_within_2h": tickets_resolved_within_2h,
                "tickets_resolved_2_to_24h": tickets_resolved_2_to_24h,
                "tickets_resolved_1_to_2d": tickets_resolved_1_to_2d,
                "tickets_resolved_after_2d": tickets_resolved_after_2d,
                "open_ticket_age_count": open_ticket_age_count,
                "avg_open_ticket_age_hours": avg_open_ticket_age_hours,

                # Assigned-building aliases for dashboard/API compatibility
                "assigned_building_ticket_resolution_count": ticket_resolution_count,
                "assigned_building_avg_ticket_resolution_hours": avg_ticket_resolution_hours,
                "assigned_building_median_ticket_resolution_hours": median_ticket_resolution_hours,

                # Backward-compatible keys
                "reopened": tickets_reopened,
                "avg_ticket_rating": avg_ticket_rating,
                "ticket_rating_sum": ticket_rating_sum,
                "ticket_rating_count": ticket_rating_count,
                "tickets_rated": tickets_rated,
                "tickets_unrated": tickets_unrated,
                "ticket_rating_coverage_pct": ticket_rating_coverage_pct,
            }
        ),  
        "property_management": _compact(
            {
                "assigned_buildings": assigned_buildings,
                "vacant_properties": vacant_properties,
                "property_marks_by_staff": _int(counts.get("property_marks_by_staff")),

                # Backward-compatible received counts
                "checkin_feedback": checkin_feedback_received,
                "checkout_feedback": checkout_feedback_received,

                # Manager dashboard denominator fields
                "checkin_feedback_received": checkin_feedback_received,
                "checkin_feedback_total": checkin_feedback_total,
                "checkin_feedback_missing": checkin_feedback_missing,
                "checkin_feedback_coverage_pct": checkin_feedback_coverage_pct,

                "checkout_feedback_received": checkout_feedback_received,
                "checkout_feedback_total": checkout_feedback_total,
                "checkout_feedback_missing": checkout_feedback_missing,
                "checkout_feedback_coverage_pct": checkout_feedback_coverage_pct,

                # Feedback ratings
                "avg_checkin_stay_rating": avg_checkin_stay_rating,
                "avg_checkin_cleaning_rating": avg_checkin_cleaning_rating,
                "avg_checkout_rms_rating": avg_checkout_rms_rating,
                "avg_checkout_building_rating": avg_checkout_building_rating,
            }
        ),
        "workload": _compact(
            {
                "calls_per_day": round(total_calls / days, 2) if days else None,
                "site_visits_per_day": round(site_visits_total / days, 2) if days else None,
                "actual_site_visits_per_day": round(site_visits_actual / days, 2) if days else None,
                "communication_events_per_day": round((total_calls + whatsapp_total) / days, 2) if days else None,
                "total_visible_activity_units": total_calls + whatsapp_total + site_visits_total + tickets_total,
                "visible_activity_units_per_day": round(
                    (total_calls + whatsapp_total + site_visits_total + tickets_total) / days,
                    2,
                )
                if days
                else None,
            }
        ),
        "context_tables": {
            "assigned_buildings": activity.get("assigned_buildings") or [],
            "vacant_properties": activity.get("vacant_properties") or [],
            "top_call_counterparties": _top_call_counterparties(activity, limit=10),
            "recent_site_visits": _recent_site_visits(activity, limit=20),
            "risky_site_visits": _risky_site_visits(activity, limit=50),
        },
        "data_visibility": {
            "has_calls": total_calls > 0,
            "has_whatsapp": whatsapp_total > 0,
            "has_tickets": tickets_total > 0,
            "has_site_visits": site_visits_total > 0,
            "has_assigned_buildings": assigned_buildings > 0,
            "has_vacant_properties": vacant_properties > 0,
            "has_property_marks": _int(counts.get("property_marks_by_staff")) > 0,
            "has_checkin_feedback": checkin_feedback_received > 0,
            "has_checkout_feedback": checkout_feedback_received > 0,
            "has_checkin_feedback_due": checkin_feedback_total > 0,
            "has_checkout_feedback_due": checkout_feedback_total > 0,
        },
    }
