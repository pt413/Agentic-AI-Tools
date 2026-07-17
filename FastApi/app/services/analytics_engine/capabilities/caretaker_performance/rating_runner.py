from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_engine.capabilities.staff_activity_review import StaffActivityReviewService

from .analytics_builder import build_caretaker_analytics
from .cache import (
    get_caretaker_performance_cache,
    make_cache_key,
    store_caretaker_performance_cache,
)
from .llm_client import run_caretaker_prompt, stable_json_hash
from .parsing import parse_caretaker_performance_review

DEFAULT_SCHEMA = "AnalyticsEngine"

CARETAKER_PERFORMANCE_CONTEXT_VERSION = "caretaker_performance_review:v6"
CARETAKER_PERFORMANCE_DEFAULT_DAYS = 30


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


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return 0


def _fmt_pct(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return "0"

    if number.is_integer():
        return str(int(number))

    return f"{number:.2f}".rstrip("0").rstrip(".")


def _site_visit_risk_numbers(analytics: dict[str, Any]) -> dict[str, Any]:
    site_visits = (
        analytics.get("site_visits")
        if isinstance(analytics.get("site_visits"), dict)
        else {}
    )
    communication = (
        analytics.get("communication")
        if isinstance(analytics.get("communication"), dict)
        else {}
    )

    missed_no_followup = _int(site_visits.get("not_done_missed_call_no_connected_followup"))
    no_booking_no_call = _int(site_visits.get("not_done_no_booking_no_call_activity"))
    risky = missed_no_followup + no_booking_no_call

    return {
        "risky": risky,
        "missed_no_followup": missed_no_followup,
        "no_booking_no_call": no_booking_no_call,
        "missed_rate_pct": communication.get("missed_rate_pct"),
    }


def _deterministic_main_reason(analytics: dict[str, Any]) -> str | None:
    risk = _site_visit_risk_numbers(analytics)
    risky = _int(risk.get("risky"))
    missed_rate_pct = risk.get("missed_rate_pct")

    if risky <= 0:
        return None

    if missed_rate_pct not in (None, ""):
        return (
            f"{risky} risky not-done site visits and "
            f"{_fmt_pct(missed_rate_pct)}% missed/zero-duration call rate."
        )

    return f"{risky} risky not-done site visits."


def _deterministic_site_visit_action(analytics: dict[str, Any]) -> dict[str, Any] | None:
    risk = _site_visit_risk_numbers(analytics)
    risky = _int(risk.get("risky"))

    if risky <= 0:
        return None

    missed_no_followup = _int(risk.get("missed_no_followup"))
    no_booking_no_call = _int(risk.get("no_booking_no_call"))

    return {
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


def _deterministic_action_rows(
    analytics: dict[str, Any],
    action_rows: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    deterministic_action = _deterministic_site_visit_action(analytics)
    rows = [row for row in (action_rows or []) if isinstance(row, dict)]

    if not deterministic_action:
        return rows

    filtered_rows = []
    for row in rows:
        action_text = str(row.get("action") or "").lower()
        evidence_text = str(row.get("evidence") or "").lower()
        is_site_visit_risk_action = (
            "risky not-done site visit" in action_text
            or "not_done_missed_call_no_connected_followup" in evidence_text
            or "not_done_no_booking_no_call_activity" in evidence_text
        )
        if not is_site_visit_risk_action:
            filtered_rows.append(row)

    return [deterministic_action, *filtered_rows]


def _apply_deterministic_overrides(parsed: dict[str, Any], analytics: dict[str, Any]) -> dict[str, Any]:
    corrected = dict(parsed or {})

    main_reason = _deterministic_main_reason(analytics)
    if main_reason:
        corrected["main_reason"] = main_reason

    corrected["action_rows"] = _deterministic_action_rows(
        analytics,
        corrected.get("action_rows") if isinstance(corrected.get("action_rows"), list) else [],
    )

    return corrected


def _staff_identity(activity: dict[str, Any]) -> dict[str, Any]:
    staff = activity.get("staff") if isinstance(activity.get("staff"), dict) else {}
    input_data = activity.get("input") if isinstance(activity.get("input"), dict) else {}

    return _compact_dict(
        {
            "username": staff.get("username") or input_data.get("username"),
            "email": staff.get("email") or input_data.get("email"),
            "phone": input_data.get("phone") or staff.get("phone"),
            "staff_team": staff.get("team") or input_data.get("staff_team"),
            "role_scope": activity.get("role_scope") or input_data.get("resolved_role_scope"),
        }
    )


def _window_payload(activity: dict[str, Any], days: int) -> dict[str, Any]:
    window = activity.get("window") if isinstance(activity.get("window"), dict) else {}

    return _compact_dict(
        {
            "window_days": int(days or CARETAKER_PERFORMANCE_DEFAULT_DAYS),
            "window_start": window.get("start"),
            "window_end": window.get("end"),
            "window_label": window.get("label"),
        }
    )


def _context_hash_for_analytics(analytics: dict[str, Any]) -> str:
    stable_analytics = dict(analytics or {})
    stable_analytics.pop("window", None)

    return stable_json_hash(
        {
            "context_version": CARETAKER_PERFORMANCE_CONTEXT_VERSION,
            "analytics": stable_analytics,
        }
    )

def _cached_output(
    cached: dict[str, Any],
    *,
    cache_status: str,
    started: float,
    activity: dict[str, Any] | None = None,
    prompt: str | None = None,
    current_context_hash: str | None = None,
) -> dict[str, Any]:
    out = dict(cached)
    out.update(
        {
            "view": "caretaker_performance_rating",
            "cached": True,
            "cache_status": cache_status,
            "llm_called": False,
            "current_context_hash": current_context_hash,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    )

    if activity is not None:
        out["activity"] = activity

    if prompt is not None:
        out["llm_prompt"] = prompt

    return _compact_dict(out)


def _metrics_only_payload(
    *,
    cache_key: str,
    identity: dict[str, Any],
    window: dict[str, Any],
    analytics: dict[str, Any],
    context_hash: str,
    started: float,
    cached: dict[str, Any] | None = None,
    cache_status: str = "metrics_only_stored",
    activity: dict[str, Any] | None = None,
    prompt: str | None = None,
) -> dict[str, Any]:
    had_cached_rating = bool(cached)
    stale_rating_message = (
        "Metrics refreshed without LLM. Run with run_llm=true to regenerate score, risk, actions, and narrative."
    )
    return _compact_dict(
        {
            "view": "caretaker_performance_rating",
            "cache_key": cache_key,
            **identity,
            **window,
            "status": "ok",
            "model": None,
            "context_version": CARETAKER_PERFORMANCE_CONTEXT_VERSION,
            "context_hash": context_hash,
            "overall_score": None,
            "priority_score": None,
            "communication_score": None,
            "site_visit_score": None,
            "ticket_score": None,
            "management_score": None,
            "overall_risk": "Metrics Only",
            "main_reason": stale_rating_message,
            "summary": {
                "staff": identity,
                "window": window,
                "analytics": analytics,
                "rating_status": "metrics_only",
                "previous_rating_cleared": had_cached_rating,
            },
            "metrics": analytics,
            "action_rows": [],
            "review_text": stale_rating_message,
            "cached": had_cached_rating,
            "cache_status": cache_status,
            "llm_called": False,
            "activity": activity,
            "llm_prompt": prompt,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    )


def _is_insufficient_caretaker_evidence(analytics: dict[str, Any]) -> bool:
    """
    True no-data case only.

    If ticket/call/visit/building/property-mark/check-in/check-out evidence exists,
    this should return False so the row can be rated or marked Needs Review,
    not No Data.
    """
    communication = analytics.get("communication") if isinstance(analytics.get("communication"), dict) else {}
    site_visits = analytics.get("site_visits") if isinstance(analytics.get("site_visits"), dict) else {}
    tickets = analytics.get("tickets") if isinstance(analytics.get("tickets"), dict) else {}
    property_management = (
        analytics.get("property_management")
        if isinstance(analytics.get("property_management"), dict)
        else {}
    )

    assigned_buildings = _num(property_management.get("assigned_buildings"))
    vacant_properties = _num(property_management.get("vacant_properties"))

    total_calls = _num(communication.get("total_calls"))
    whatsapp_total = _num(communication.get("whatsapp_total"))
    whatsapp_messages = _num(communication.get("whatsapp_messages"))
    internal_calls = _num(communication.get("internal_calls"))
    external_calls = _num(communication.get("external_calls"))

    site_visit_total = _num(site_visits.get("total"))
    actual_staff_visits = _num(site_visits.get("actual_staff_visits"))
    system_scheduled_visits = _num(site_visits.get("system_scheduled_visits"))

    tickets_total = _num(tickets.get("total"))
    tickets_open = _num(tickets.get("open"))
    tickets_closed = _num(tickets.get("closed"))
    tickets_reopened = _num(tickets.get("reopened"))
    tickets_closed_in_window = _num(tickets.get("closed_in_window"))
    own_tickets_closed = _num(tickets.get("own_closed_in_window"))
    own_closed_in_window = _num(tickets.get("own_closed"))

    property_marks = _num(property_management.get("property_marks_by_staff"))
    checkin_feedback = _num(property_management.get("checkin_feedback"))
    checkout_feedback = _num(property_management.get("checkout_feedback"))

    visible_activity_units = (
        assigned_buildings
        + vacant_properties
        + total_calls
        + whatsapp_total
        + whatsapp_messages
        + internal_calls
        + external_calls
        + site_visit_total
        + actual_staff_visits
        + system_scheduled_visits
        + tickets_total
        + tickets_open
        + tickets_closed
        + tickets_reopened
        + tickets_closed_in_window
        + own_tickets_closed
        + own_closed_in_window
        + property_marks
        + checkin_feedback
        + checkout_feedback
    )

    return visible_activity_units <= 0


def _insufficient_evidence_payload(
    *,
    cache_key: str,
    identity: dict[str, Any],
    window: dict[str, Any],
    analytics: dict[str, Any],
    context_hash: str,
    include_activity: bool,
    activity: dict[str, Any],
    include_prompt: bool,
    prompt: str,
    started: float,
) -> dict[str, Any]:
    reason = (
        "No assigned buildings, vacant properties, calls, WhatsApp activity, "
        "site visits, tickets, property marks, or feedback records were found "
        "in the selected 30-day evidence window."
    )

    return {
        "view": "caretaker_performance_rating",
        "cache_key": cache_key,
        **identity,
        **window,
        "status": "ok",
        "model": None,
        "context_version": CARETAKER_PERFORMANCE_CONTEXT_VERSION,
        "context_hash": context_hash,
        "overall_score": None,
        "priority_score": None,
        "communication_score": None,
        "site_visit_score": None,
        "ticket_score": None,
        "management_score": None,
        "overall_risk": "No Data",
        "main_reason": reason,
        "summary": {
            "staff": identity,
            "window": window,
            "analytics": analytics,
            "rating_status": "insufficient_data",
            "rating_skipped_reason": reason,
        },
        "metrics": analytics,
        "action_rows": [
            {
                "priority_score": None,
                "owner_team": "Manager",
                "action": "Verify caretaker mapping and activity attribution.",
                "evidence": reason,
            }
        ],
        "review_text": (
            "Overall verdict:\n"
            "Score: Not rated\n"
            "Priority score: Not rated\n"
            "Communication score: Not rated\n"
            "Site visit score: Not rated\n"
            "Ticket score: Not rated\n"
            "Management score: Not rated\n"
            "Risk: No Data\n"
            f"Main reason: {reason}\n\n"
            "LLM review was skipped because there is insufficient visible evidence."
        ),
        "cached": False,
        "cache_status": "insufficient_data_stored",
        "llm_called": False,
        "activity": activity if include_activity else None,
        "llm_prompt": prompt if include_prompt else None,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def _build_prompt(analytics: dict[str, Any]) -> str:
    caretaker = analytics.get("caretaker") if isinstance(analytics.get("caretaker"), dict) else {}
    username = caretaker.get("username") or "-"

    site_visit_guidance = """
Site visit interpretation rules:
- Python has already mapped caretaker ownership using assigned building_id -> caretaker, not lead assignment and not executive_id.
- schedule_status = 0 is counted as done_visits / actual_staff_visits. This means the visit was actually completed.
- schedule_status = 1 is counted as scheduled_not_done_visits / system_scheduled_visits. This means the visit was scheduled but not completed.
- Do not judge site visit quality only from done_visit_rate_pct or actual_visit_rate_pct.
- Do not punish the whole scheduled_not_done_visits count.
- First split scheduled-not-done visits into explainable/recovered/risky buckets.

Bucket meaning:
- not_done_due_to_booking_full means the property had overlapping successful booking/full evidence. This is usually neutral/explainable. Do not treat these rows as caretaker failure.
- not_done_with_connected_followup means the visit was not completed, but connected call follow-up exists near the scheduled visit. Treat this as recovered/partially handled, not as severe failure.
- not_done_missed_call_no_connected_followup means a missed/zero-duration call happened near the visit and there was no connected follow-up after the last missed call. This is a negative site-visit follow-up signal.
- not_done_no_booking_no_call_activity means there was no booking/full explanation and no visible call activity near the scheduled visit. This is a strong negative site-visit signal.
- not_done_unknown_reason is only a data-quality fallback. Mention uncertainty; do not strongly punish unless supported by other evidence.

Risk calculation:
- explainable_site_visit_rows = not_done_due_to_booking_full
- recovered_site_visit_rows = not_done_with_connected_followup
- risky_site_visit_rows = not_done_missed_call_no_connected_followup + not_done_no_booking_no_call_activity
- Site visit score should mainly penalize risky_site_visit_rows, not explainable_site_visit_rows.
- If booking/full rows are high, mention them as availability/scheduling context, not as caretaker negligence.
- If connected-follow-up rows are high, mention them as partial recovery, not as complete failure.
- If a row has both booking overlap and missed-call-no-follow-up evidence, the deterministic reason bucket intentionally prioritizes missed-call-no-follow-up because communication recovery is still required after a missed caretaker/admin-side call.

Required wording:
- Do not write: "engage with properties that had booking overlaps" as a corrective action.
- Correct action focus: "Audit the risky site-visit rows: missed-call/no-connected-follow-up and no-booking/no-call-activity."
- Correct evidence format: "{risky_site_visit_rows} risky not-done visits = {not_done_missed_call_no_connected_followup} missed-call/no-connected-follow-up + {not_done_no_booking_no_call_activity} no-booking/no-call-activity."
""".strip()

    scoring_guidance = """
Scoring guidance:
- Overall score should combine communication quality, site visit handling, ticket handling, property-management evidence, and workload context.
- Priority score is urgency/action priority, not the same as quality. High priority should mean manager action is needed soon.
- Communication score should reflect connect rate, missed/zero-duration call rate, external/customer follow-up, and verified recovery metrics where available.
- Site visit score should be based on done visits plus reason-bucket quality for scheduled-not-done visits. Do not blindly punish all scheduled-not-done visits.
- Ticket score should consider ticket volume, closure activity, reopening, ticket rating/feedback, and whether tickets are clearly staff-owned or only assigned-building/property-context metrics.
- Management score should consider assigned buildings, check-in/checkout feedback, property marks, vacant-property context, and visible field work.
- If metrics are strong in one area but weak in another, make that clear instead of averaging everything into a vague middle score.
""".strip()

    rating_required_guidance = """
Rating requirement:
- The backend has already skipped true no-data cases before calling the LLM.
- If this prompt reaches you and the metrics show any assigned_buildings, calls, site_visits, tickets, feedback, or property marks, you must return numeric scores.
- Do not return Score: Not rated unless all evidence is genuinely absent.
- Do not return Priority score: Not rated unless all evidence is genuinely absent.
- If data_visibility.has_assigned_buildings = true, do not say there are no assigned buildings.
- If data_visibility.has_calls = true, do not say there is no visible caretaker activity.
- If data_visibility.has_site_visits = true, do not say there is no visible site visit activity.
- If data_visibility.has_tickets = true, describe the ticket evidence according to the ticket attribution rule.
- Overall score and Priority score must be numeric whenever Communication/Site visit/Ticket/Management scores are numeric.
- The main reason must be based on visible verified metrics.
- If risky site-visit buckets are non-zero, the main reason must mention risky_site_visit_rows.
- Never write "missed calls with no connected follow-up" unless a global missed_without_connected_followup metric is explicitly present.
- If only missed_or_zero_duration_calls is present, describe it as "high missed/zero-duration call rate", not "missed calls with no follow-up".
- Correct main reason format: "{risky_site_visit_rows} risky not-done site visits and high missed/zero-duration call rate."
- Do not say "significant scheduled-not-done visits without connected follow-up" unless all scheduled_not_done_visits are actually unrecovered.
- Never use a no-data main reason when data_visibility shows visible evidence.
""".strip()

    missed_call_guidance = """
Missed-call interpretation rules:
- missed_or_zero_duration_calls is a general communication-volume metric. It does not mean every missed call required follow-up.
- Do not say all missed_or_zero_duration_calls had no follow-up unless a verified unrecovered-missed-call metric is provided.
- General missed calls should affect communication score only as a broad signal, especially when missed_rate_pct is high.
- If a global missed-call recovery metric is available, use it carefully:
  * missed_with_connected_followup_24h means missed calls that were recovered by a connected call within 24 hours.
  * missed_without_connected_followup_24h means missed calls with no connected follow-up within 24 hours.
  * followup_recovery_rate_pct means recovery rate for missed/zero-duration calls.
- Even missed_without_connected_followup_24h is still general communication evidence. Do not automatically treat every unrecovered missed call as a site-visit failure.
- For caretaker site-visit accountability, use only the site-visit-specific deterministic buckets:
  * not_done_missed_call_no_connected_followup
  * not_done_no_booking_no_call_activity
- The strongest site-visit penalty should come from:
  risky_site_visit_rows = not_done_missed_call_no_connected_followup + not_done_no_booking_no_call_activity.
- If not_done_missed_call_no_connected_followup is 15 and missed_or_zero_duration_calls is 507, do not write "507 missed calls with no follow-up."
- Correct wording example: "507 missed/zero-duration calls overall, but the verified site-visit follow-up concern is 15 not-done site visits with missed-call/no-connected-follow-up evidence, plus 12 not-done visits with no booking/no call activity."
""".strip()

    ticket_guidance = """
Ticket attribution rule:
- tickets.total, tickets.closed, closed_in_window, own_closed_in_window, assigned_building_tickets_total, assigned_building_tickets_closed_in_window, and avg_ticket_rating are assigned-building/property-context ticket metrics unless staff_resolved_tickets is explicitly provided.
- Do not say the caretaker personally closed, personally managed, or personally handled all tickets just because assigned-building tickets are closed.
- If staff_resolved_tickets is not available, describe tickets as "tickets visible for assigned buildings/properties", not "tickets personally handled by caretaker".
- Use avg_ticket_rating and reopened tickets as quality signals, but mention attribution uncertainty when staff-level ticket ownership is not explicitly available.
- Low avg_ticket_rating should prevent a very high ticket score unless there is strong staff-owned resolution evidence.
- Do not call assigned-building ticket closure "strong ticket handling" unless staff_resolved_tickets or staff-owned ticket evidence is explicitly provided.
- Correct wording example: "735 assigned-building/property-context tickets were visible and closed, but personal attribution is uncertain; avg_ticket_rating = 2.86 and reopened = 3 indicate quality concerns."
- Do not write: "735 tickets were handled/managed/closed by the caretaker" unless staff-owned ticket evidence is explicitly present.
""".strip()

    action_guidance = """
Manager action rules:
- Action rows must target the actual risky bucket, not explainable bucket counts.
- If site-visit risky buckets are non-zero, create the first action focused on:
  * not_done_missed_call_no_connected_followup
  * not_done_no_booking_no_call_activity
- Required first site-visit action wording:
  "Audit and recover risky not-done site visits: missed-call/no-connected-follow-up and no-booking/no-call-activity."
- Required first site-visit evidence wording:
  "{risky_site_visit_rows} risky not-done visits = {not_done_missed_call_no_connected_followup} not_done_missed_call_no_connected_followup + {not_done_no_booking_no_call_activity} not_done_no_booking_no_call_activity."
- Do not create an action saying the caretaker should engage with booking/full properties. Booking/full rows are usually explainable availability conflicts.
- Do not treat not_done_due_to_booking_full as direct caretaker failure.
- For booking/full rows, create only a low/medium priority scheduling action if needed:
  "Improve scheduling/availability visibility so booked/full properties are not repeatedly scheduled for visits."
- Booking/full action owner should be "Operations/Scheduling", not "Caretaker Team", unless evidence shows caretaker caused the availability mismatch.
- For missed-call actions, do not cite total missed_or_zero_duration_calls as "no follow-up".
- If discussing site-visit missed follow-up, cite not_done_missed_call_no_connected_followup.
- If discussing general communication, cite missed_or_zero_duration_calls and missed_rate_pct only as broad communication-volume/rate evidence.
- Ticket actions must say "assigned-building/property-context tickets" unless staff-owned ticket metrics are explicitly available.
- Evidence should reference exact metric names/counts, with underscores preserved.
""".strip()

    return (
        f"Below is the staff activity evidence from AnalyticsEngine for Caretaker: {username}.\n\n"
        "Please analyse this as an individual staff/admin activity review for role scope: Caretaker.\n\n"
        "Important guardrails:\n"
        "- Use only the evidence below; do not invent facts.\n"
        "- Python has already computed the mathematical metrics and deterministic reason buckets. Your job is to analyse performance, explain business meaning, and give manager actions.\n"
        "- Do not recalculate or override deterministic counts. Use the provided counts and reason buckets as source of truth.\n"
        "- Common evidence for every role is only calls and WhatsApp from the staff/user number or assigned pooled line.\n"
        "- Communication connect rate means visible connected calls out of total calls involving the caretaker line.\n"
        "- Separate internal coordination from external/customer communication.\n"
        "- Do not judge this staff member on role-specific sections that are missing or not applicable.\n"
        "- If a source/table appears missing or empty, explicitly say that the evidence is unavailable rather than assuming no work happened.\n"
        "- Building/property context is intentionally compact: assigned buildings show only BuildName and BuildingId; vacant properties show only property_name and vacant_since.\n"
        "- Do not infer missing work from a missing role-specific section. Treat absent sections as not visible in provided data.\n"
        "- Do not give a neutral/middle score such as 5/10 only because evidence is missing.\n"
        "- True no-data cases are handled before this LLM prompt is called. If visible metrics are present, do not return Not rated or No Data.\n"
        "- If only very small evidence is visible, score conservatively and clearly say the rating confidence is low.\n\n"
        f"{ticket_guidance}\n\n"
        "- Ticket interpretation rule:\n"
        "  * If assigned buildings/properties exist and operational activity is visible, low or zero tickets can indicate stable property management.\n"
        "  * If tickets exist, judge based on closure context, reopening patterns, ticket rating/feedback, and whether the caretaker appears explicitly responsible for the ticket.\n"
        "  * Do not penalize caretakers simply because ticket count is low.\n"
        "  * Do not reward a caretaker heavily for closed assigned-building tickets unless the visible evidence clearly shows the caretaker personally handled them.\n\n"
        "No-data rule:\n"
        "- The Python backend already checks true no-data cases and skips LLM review before this prompt is sent.\n"
        "- Therefore, if this prompt reaches you and metrics/data_visibility show any visible evidence, return numeric scores and do not use Risk: No Data.\n"
        "- Only use Score: Not rated / Risk: No Data if the supplied metrics themselves show no assigned buildings, no calls, no WhatsApp activity, no site visits, no tickets, no feedback, and no property marks.\n\n"
        "Role-specific focus:\n"
        "- Assigned building/property coverage and current availability awareness.\n"
        "- Track non-currently-available units separately as availability-date/current-occupancy inventory; count against quality only when avl_date has passed, checkout is completed, and the unit is still not bookable.\n"
        "- Check-in/checkout feedback, cleaning/stay/building ratings, and property verification marks.\n"
        "- Assigned-building/property ticket context plus field communication and follow-up quality; only call tickets 'own tickets' when staff-level ownership is explicit.\n"
        "- Site visit quality must use the deterministic not-done reason buckets below. Low done_visit_rate_pct alone should not automatically produce a low score if many not-done visits were property-booked/full or had connected follow-up.\n"
        "- Communication quality: high missed/zero-duration calls should reduce communication score as a broad signal, but do not claim every missed call lacked follow-up unless verified recovery metrics prove it.\n\n"
        f"{site_visit_guidance}\n\n"
        f"{scoring_guidance}\n\n"
        f"{rating_required_guidance}\n\n"
        f"{missed_call_guidance}\n\n"
        f"{action_guidance}\n\n"
        "Review checklist:\n"
        "1. Overall work coverage for the selected role scope.\n"
        "2. Communication: internal vs external calls, office numbers used, WhatsApp direct/group messages, connect rate, missed/zero-duration call rate, verified recovery metrics where available, and communication gaps.\n"
        "3. Site visits: separate done visits, property-booked/full not-done visits, connected-follow-up not-done visits, missed-call-no-follow-up not-done visits, and no-booking-no-call-activity not-done visits.\n"
        "4. Role-specific evidence only: field feedback/site visits/property marks/tickets for Caretaker where present.\n"
        "5. Assigned building table and vacant property table are context only; do not expand them into unrelated property-quality claims.\n"
        "6. What was handled correctly, what needs immediate improvement, and what should be followed up.\n"
        "7. Give a practical rating out of 10 only when there is enough visible evidence.\n"
        "8. Suggest exact next actions for the manager.\n\n"
        "Return exactly:\n"
        "1. Overall verdict:\n"
        "Score: X/10 or Not rated\n"
        "Priority score: X/10 or Not rated\n"
        "Important: If visible evidence exists, Score and Priority score must be numeric, not Not rated.\n"
        "Communication score: X/10 or Not rated\n"
        "Site visit score: X/10 or Not rated\n"
        "Ticket score: X/10 or Not rated\n"
        "Management score: X/10 or Not rated\n"
        "Risk: Low / Medium / High / No Data\n"
        "Main reason: one concise reason.\n"
        "- Main reason must not say missed calls had no connected follow-up unless a global missed_without_connected_followup metric is explicitly present.\n"
        "- If risky site-visit buckets are non-zero, use wording like: '{risky_site_visit_rows} risky not-done site visits and high missed/zero-duration call rate.'\n"
        "- Main reason must mention risky_site_visit_rows when risky site-visit buckets are non-zero.\n"
        "- Main reason must not blame all scheduled_not_done_visits when many are booking/full or connected-follow-up recovered.\n\n"
        "2. Performance analysis: concise bullets for communication, site visits, tickets, property management, workload.\n"
        "- In the communication bullet, do not describe total missed_or_zero_duration_calls as unrecovered/no-follow-up unless a verified unrecovered metric is present.\n"
        "- In the site visit bullet, explicitly mention the not-done reason bucket counts when available.\n"
        "- Separate explainable/recovered not-done visits from risky not-done visits.\n"
        "- In the ticket bullet, explicitly say whether ticket metrics are staff-owned or only assigned-building/property-context metrics.\n\n"
        "3. Immediate manager actions:\n"
        "| Priority score /10 | Owner/team | Action | Evidence |\n"
        "|---:|---|---|---|\n"
        "- Include actions for risky site-visit buckets if they are non-zero.\n"
        "- Do not recommend engaging with booking/full properties as if booking/full is caretaker failure.\n"
        "- For booking/full conflicts, only suggest scheduling/availability visibility improvement if needed.\n"
        "- For missed-call actions, do not cite total missed_or_zero_duration_calls as 'no follow-up'.\n"
        "- If discussing site-visit missed follow-up, cite not_done_missed_call_no_connected_followup.\n"
        "- If discussing general communication, cite missed_or_zero_duration_calls and missed_rate_pct only as broad communication-volume/rate evidence.\n"
        "- Ticket action evidence must say assigned-building/property-context if staff-owned ticket metrics are not available.\n"
        "- Evidence should reference exact metric names/counts, not vague claims.\n\n"
        "CARETAKER PERFORMANCE METRICS:\n"
        + json.dumps(analytics, ensure_ascii=False, default=str, indent=2)
    )

class CaretakerPerformanceReviewRunner:
    def __init__(self, db: Session, schema: str = DEFAULT_SCHEMA):
        self.db = db
        self.schema = schema

    def build_activity(
        self,
        *,
        username: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        days: int = CARETAKER_PERFORMANCE_DEFAULT_DAYS,
        limit: int = 10000,
        print_limit: int = 80,
        max_text: int = 180,
    ) -> dict[str, Any]:
        safe_days = int(days or CARETAKER_PERFORMANCE_DEFAULT_DAYS)

        service = StaffActivityReviewService(db=self.db, schema=self.schema)

        activity = service.build_staff_activity(
            username=username,
            email=email,
            phone=phone,
            role="auto",
            days=safe_days,
            limit=int(limit or 10000),
            print_limit=int(print_limit or 80),
            max_text=int(max_text or 180),
            llm=True,
            display_mode="llm",
        )

        role_scope = str(
            activity.get("role_scope")
            or (activity.get("input") or {}).get("resolved_role_scope")
            or ""
        ).strip().lower()

        staff_team = str(
            ((activity.get("staff") or {}).get("team"))
            or ((activity.get("input") or {}).get("staff_team"))
            or ""
        ).strip().lower()

        if role_scope != "caretaker" and staff_team != "caretaker":
            raise ValueError(
                "This endpoint is only for caretaker performance. "
                f"Resolved role_scope={role_scope or '-'}, staff_team={staff_team or '-'}"
            )

        return activity

    def review_one(
        self,
        *,
        username: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        days: int = CARETAKER_PERFORMANCE_DEFAULT_DAYS,
        model: str = "gpt-5-mini",
        timeout_seconds: int = 120,
        limit: int = 10000,
        print_limit: int = 80,
        max_text: int = 180,
        run_llm: bool = False,
        use_cache: bool = True,
        force_refresh: bool = False,
        include_activity: bool = False,
        include_prompt: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        safe_days = int(days or CARETAKER_PERFORMANCE_DEFAULT_DAYS)

        activity = self.build_activity(
            username=username,
            email=email,
            phone=phone,
            days=safe_days,
            limit=limit,
            print_limit=print_limit,
            max_text=max_text,
        )

        identity = _staff_identity(activity)
        window = _window_payload(activity, safe_days)
        analytics = build_caretaker_analytics(activity)
        context_hash = _context_hash_for_analytics(analytics)

        cache_key = make_cache_key(
            username=identity.get("username"),
            email=identity.get("email") or email,
            phone=identity.get("phone") or phone,
            days=safe_days,
        )

        full_prompt_for_debug = _build_prompt(analytics) if include_prompt else ""

        if _is_insufficient_caretaker_evidence(analytics):
            payload = _insufficient_evidence_payload(
                cache_key=cache_key,
                identity=identity,
                window=window,
                analytics=analytics,
                context_hash=context_hash,
                include_activity=include_activity,
                activity=activity,
                include_prompt=include_prompt,
                prompt=full_prompt_for_debug,
                started=started,
            )

            if use_cache:
                store_caretaker_performance_cache(self.db, self.schema, payload=payload)

            return payload

        cached: dict[str, Any] | None = None
        if use_cache and not force_refresh:
            cached = get_caretaker_performance_cache(
                self.db,
                self.schema,
                cache_key=cache_key,
                ok_only=True,
            )

        prompt = _build_prompt(analytics) if include_prompt else None

        if cached:
            cached_hash = str(cached.get("context_hash") or "").strip()
            current_hash = str(context_hash or "").strip()
            cached_risk = str(cached.get("overall_risk") or "").strip().lower()
            cached_score = cached.get("overall_score")

            cached_wrongly_marked_no_data = (
                cached_risk in {"no data", "nodata", "insufficient_data"}
                and not _is_insufficient_caretaker_evidence(analytics)
            )

            cached_parse_incomplete = (
                not _is_insufficient_caretaker_evidence(analytics)
                and (cached_score is None or cached_score == "")
                and not cached_risk
            )

            if (
                cached_hash
                and current_hash
                and cached_hash == current_hash
                and not cached_wrongly_marked_no_data
                and not cached_parse_incomplete
            ):
                return _cached_output(
                    cached,
                    cache_status="hit_unchanged",
                    started=started,
                    activity=activity if include_activity else None,
                    prompt=prompt,
                    current_context_hash=context_hash,
            )

            if not run_llm:
                payload = _metrics_only_payload(
                    cache_key=cache_key,
                    identity=identity,
                    window=window,
                    analytics=analytics,
                    context_hash=context_hash,
                    started=started,
                    cached=cached,
                    cache_status="metrics_only_stored_context_changed",
                    activity=activity if include_activity else None,
                    prompt=prompt,
                )
                if use_cache:
                    store_caretaker_performance_cache(self.db, self.schema, payload=payload)
                return payload

        if not run_llm:
            payload = _metrics_only_payload(
                cache_key=cache_key,
                identity=identity,
                window=window,
                analytics=analytics,
                context_hash=context_hash,
                started=started,
                cached=None,
                cache_status="metrics_only_stored",
                activity=activity if include_activity else None,
                prompt=prompt,
            )
            if use_cache:
                store_caretaker_performance_cache(self.db, self.schema, payload=payload)
            return payload

        full_prompt = _build_prompt(analytics)

        review_text = None
        error = None

        try:
            review_text = run_caretaker_prompt(
                full_prompt,
                model=model,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"

        if error:
            payload = _metrics_only_payload(
                cache_key=cache_key,
                identity=identity,
                window=window,
                analytics=analytics,
                context_hash=context_hash,
                started=started,
                cached=cached,
                cache_status="metrics_only_stored_llm_failed",
                activity=activity if include_activity else None,
                prompt=full_prompt if include_prompt else None,
            )
            payload["llm_error"] = error

            if use_cache:
                store_caretaker_performance_cache(self.db, self.schema, payload=payload)

            return payload

        parsed = parse_caretaker_performance_review(review_text or "") if review_text else {}

        parse_incomplete = bool(
            review_text
            and not error
            and (
                parsed.get("overall_score") is None
                or not parsed.get("overall_risk")
            )
        )

        if parse_incomplete and not parsed.get("overall_risk"):
            parsed["overall_risk"] = "Needs Review"

        if parse_incomplete and not parsed.get("main_reason"):
            parsed["main_reason"] = (
                "LLM response was received, but score/risk could not be parsed reliably. "
                "Open the review text and rerun if needed."
            )

        parsed = _apply_deterministic_overrides(parsed, analytics)

        payload = _compact_dict(
            {
                "view": "caretaker_performance_rating",
                "cache_key": cache_key,
                **identity,
                **window,
                "status": "ok",
                "model": model,
                "context_version": CARETAKER_PERFORMANCE_CONTEXT_VERSION,
                "context_hash": context_hash,
                "overall_score": parsed.get("overall_score"),
                "priority_score": parsed.get("priority_score"),
                "communication_score": parsed.get("communication_score"),
                "site_visit_score": parsed.get("site_visit_score"),
                "ticket_score": parsed.get("ticket_score"),
                "management_score": parsed.get("management_score"),
                "overall_risk": parsed.get("overall_risk"),
                "main_reason": parsed.get("main_reason"),
                "summary": {
                    "staff": identity,
                    "window": window,
                    "analytics": analytics,
                    "rating_status": "parse_incomplete" if parse_incomplete else "rated",
                },
                "metrics": analytics,
                "action_rows": parsed.get("action_rows") or [],
                "review_text": review_text,
                "cached": False,
                "cache_status": (
                    "parse_incomplete_stored"
                    if parse_incomplete
                    else "stored"
                ),
                "llm_called": True,
                "activity": activity if include_activity else None,
                "llm_prompt": full_prompt if include_prompt else None,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        )

        if use_cache and not error:
            store_caretaker_performance_cache(self.db, self.schema, payload=payload)

        return payload
