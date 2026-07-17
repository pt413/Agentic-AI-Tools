from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Optional

from app.services.analytics_engine.capabilities.staff_activity_common import (
    coerce_datetime,
    compact_dict,
    lower_ref,
    now_ist_naive,
    phone_last10,
)


WORKDAY_START = time(9, 0)
WORKDAY_END = time(18, 30)
ACTION_OWNER_BY_ROLE = {
    "sales": "Sales Manager",
    "finance": "Finance Manager",
    "caretaker": "Ops Manager",
    "ops_team": "Ops Manager",
    "onboarding": "Onboarding Manager",
    "technical": "Technical Manager",
    "marketing": "Marketing Manager",
    "generic": "Team Manager",
}
RISK_ORDER = {"high": 0, "medium": 1, "low": 2, "no data": 3, "nodata": 3}
MISSED_STATUS_TERMS = {
    "missed",
    "not answered",
    "no answer",
    "unanswered",
    "busy",
    "failed",
    "cancelled",
    "canceled",
    "not connected",
}
CONNECTED_STATUS_TERMS = {
    "connected",
    "answered",
    "completed",
    "success",
    "successful",
    "received",
}
TICKET_SLA_DAYS: dict[str, dict[str, int]] = {
    "finance": {
        "rent_refund": 2,
        "late_payment_charge": 2,
        "hard_copy_agreement": 7,
    },
    "ops": {
        "water_electricity": 1,
        "pest": 5,
        "electronics": 7,
    },
    "sales": {
        "shifting": 2,
        "booking_followup": 0,
    },
    "technical": {
        "appliance": 7,
        "internet": 2,
        "electrical": 2,
    },
}


def is_working_hours_ist(value: Any) -> bool:
    parsed = coerce_datetime(value)
    if parsed is None:
        return False
    current_time = parsed.time()
    return WORKDAY_START <= current_time <= WORKDAY_END


def end_of_working_day(value: Any) -> datetime:
    parsed = coerce_datetime(value) or now_ist_naive()
    return parsed.replace(hour=WORKDAY_END.hour, minute=WORKDAY_END.minute, second=0, microsecond=0)


def next_working_day_end(value: Any) -> datetime:
    parsed = coerce_datetime(value) or now_ist_naive()
    current = parsed + timedelta(days=1)
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current.replace(hour=WORKDAY_END.hour, minute=WORKDAY_END.minute, second=0, microsecond=0)


def risk_sort_value(value: Any) -> int:
    return RISK_ORDER.get(str(value or "").strip().lower(), 99)


def infer_ticket_sla_days(team: Any, category: Any) -> int:
    team_key = str(team or "").strip().lower()
    category_key = str(category or "").strip().lower().replace(" ", "_").replace("-", "_")
    return TICKET_SLA_DAYS.get(team_key, {}).get(category_key, 3)


def _counterparty_key(row: dict[str, Any]) -> Optional[str]:
    for value in (
        row.get("counterparty_number"),
        row.get("customer_phone"),
        row.get("phone"),
        row.get("cx_number"),
        ((row.get("to") or {}) if isinstance(row.get("to"), dict) else {}).get("number"),
        ((row.get("from") or {}) if isinstance(row.get("from"), dict) else {}).get("number"),
    ):
        last10 = phone_last10(value)
        if last10:
            return last10
    return None


def _call_is_external(row: dict[str, Any]) -> bool:
    return str(row.get("call_type") or "").strip().lower() != "internal"


def _call_is_inbound(row: dict[str, Any]) -> bool:
    flow = str(row.get("flow") or "").strip().lower()
    return flow == "counterparty_to_staff" or str(row.get("direction") or "").strip().lower() in {"incoming", "inbound", "received"}


def _call_is_outbound(row: dict[str, Any]) -> bool:
    flow = str(row.get("flow") or "").strip().lower()
    return flow == "staff_to_counterparty" or str(row.get("direction") or "").strip().lower() in {"outgoing", "outbound", "dialed"}


def _call_is_connected(row: dict[str, Any]) -> bool:
    duration = int(row.get("duration_sec") or 0)
    if duration > 0:
        return True
    status_text = str(row.get("status") or "").strip().lower()
    return status_text in CONNECTED_STATUS_TERMS


def _call_is_weak_connected(row: dict[str, Any]) -> bool:
    duration = int(row.get("duration_sec") or 0)
    text = str(row.get("transcript") or row.get("summary") or "").strip()
    return 0 < duration < 30 and not text


def _call_is_missed_inbound(row: dict[str, Any]) -> bool:
    if not _call_is_external(row) or not _call_is_inbound(row):
        return False
    if _call_is_connected(row):
        return False
    status_text = str(row.get("status") or "").strip().lower()
    duration = int(row.get("duration_sec") or 0)
    return duration <= 0 or status_text in MISSED_STATUS_TERMS


def _event_time(row: dict[str, Any]) -> Optional[datetime]:
    return coerce_datetime(row.get("time") or row.get("activity_time") or row.get("message_time") or row.get("call_time"))


def _find_recovery_event(
    *,
    events: list[dict[str, Any]],
    counterparty: Optional[str],
    missed_at: datetime,
    deadline: datetime,
) -> Optional[dict[str, Any]]:
    if not counterparty:
        return None
    for event in events:
        event_time = event.get("_time")
        if not isinstance(event_time, datetime):
            continue
        if event_time <= missed_at or event_time > deadline:
            continue
        if event.get("_counterparty") != counterparty:
            continue
        if event.get("channel") == "call" and event.get("_connected"):
            return event
        if str(event.get("flow") or "").strip().lower() == "staff_to_counterparty":
            return event
    return None


def _ticket_owner_scope(role_scope: str, category_text: str) -> str:
    role_key = str(role_scope or "").strip().lower()
    text = str(category_text or "").strip().lower()
    if role_key == "finance":
        return "finance"
    if role_key in {"caretaker", "ops_team"}:
        return "ops"
    if role_key == "technical":
        return "technical"
    if role_key == "sales":
        return "sales"
    for team_name in ("finance", "ops", "technical", "sales"):
        if team_name in text:
            return team_name
    return "ops"


def _ticket_sla_breached(ticket: dict[str, Any]) -> bool:
    created_at = coerce_datetime(ticket.get("created_at"))
    close_date = coerce_datetime(ticket.get("close_date") or ticket.get("activity_time"))
    if created_at is None or close_date is None:
        active_days = ticket.get("active_days")
        try:
            active_days_value = float(active_days)
        except Exception:
            return False
        category = str(ticket.get("category") or "").strip().lower()
        owner_scope = _ticket_owner_scope(str(ticket.get("team") or ""), category)
        return active_days_value > float(infer_ticket_sla_days(owner_scope, category))
    category = str(ticket.get("category") or "").strip().lower()
    owner_scope = _ticket_owner_scope(str(ticket.get("team") or ""), category)
    return (close_date - created_at).days > infer_ticket_sla_days(owner_scope, category)


def compute_business_signals(activity: dict[str, Any], *, now: Optional[datetime] = None) -> dict[str, Any]:
    current_time = now or now_ist_naive()
    data = activity.get("data") if isinstance(activity.get("data"), dict) else {}
    counts = activity.get("counts") if isinstance(activity.get("counts"), dict) else {}
    role_scope = str(activity.get("role_scope") or "generic")

    calls = [row for row in (data.get("calls") or []) if isinstance(row, dict)]
    whatsapp = [row for row in (data.get("whatsapp") or []) if isinstance(row, dict)]
    tickets = [row for row in (data.get("tickets") or []) if isinstance(row, dict)]
    finance_rows = [row for row in (data.get("finance_rows") or []) if isinstance(row, dict)]
    leads = [row for row in (data.get("leads") or []) if isinstance(row, dict)]
    bookings = [row for row in (data.get("bookings") or []) if isinstance(row, dict)]
    travel_cart = [row for row in (data.get("travel_cart") or []) if isinstance(row, dict)]
    site_visits = [row for row in (data.get("site_visits") or []) if isinstance(row, dict)]

    direct_whatsapp = [
        row for row in whatsapp
        if str(row.get("conversation_kind") or "").strip().lower() != "group"
    ]

    events: list[dict[str, Any]] = []
    for row in calls:
        event_time = _event_time(row)
        if event_time is None:
            continue
        events.append(
            {
                **row,
                "_time": event_time,
                "_counterparty": _counterparty_key(row),
                "_connected": _call_is_connected(row),
            }
        )
    for row in direct_whatsapp:
        event_time = _event_time(row)
        if event_time is None:
            continue
        events.append(
            {
                **row,
                "_time": event_time,
                "_counterparty": _counterparty_key(row),
            }
        )
    events.sort(key=lambda row: row.get("_time") or datetime.min)

    missed_call_count = 0
    missed_inbound_working_hours_count = 0
    recovered_missed_calls_count = 0
    unrecovered_missed_calls_count = 0
    after_hours_missed_calls_count = 0

    for row in calls:
        event_time = _event_time(row)
        if event_time is None or not _call_is_missed_inbound(row):
            continue
        missed_call_count += 1
        if is_working_hours_ist(event_time):
            missed_inbound_working_hours_count += 1
            deadline = end_of_working_day(event_time)
        else:
            after_hours_missed_calls_count += 1
            deadline = next_working_day_end(event_time)

        recovered = _find_recovery_event(
            events=events,
            counterparty=_counterparty_key(row),
            missed_at=event_time,
            deadline=deadline,
        )
        if recovered:
            recovered_missed_calls_count += 1
        else:
            unrecovered_missed_calls_count += 1

    whatsapp_response_gap_count = 0
    pending_by_counterparty: dict[str, dict[str, Any]] = {}
    for event in events:
        counterparty = event.get("_counterparty")
        if not counterparty or event.get("channel") != "whatsapp":
            if (
                counterparty
                and counterparty in pending_by_counterparty
                and event.get("channel") == "call"
                and event.get("_connected")
            ):
                pending_by_counterparty.pop(counterparty, None)
            continue

        flow = str(event.get("flow") or "").strip().lower()
        if flow == "counterparty_to_staff":
            if counterparty in pending_by_counterparty:
                continue
            event_time = event.get("_time")
            if not isinstance(event_time, datetime):
                continue
            deadline = end_of_working_day(event_time) if is_working_hours_ist(event_time) else next_working_day_end(event_time)
            pending_by_counterparty[counterparty] = {"deadline": deadline}
            continue
        if flow == "staff_to_counterparty" and counterparty in pending_by_counterparty:
            pending_by_counterparty.pop(counterparty, None)

    for counterparty, pending in pending_by_counterparty.items():
        if current_time > pending.get("deadline"):
            whatsapp_response_gap_count += 1

    ticket_sla_breach_count = sum(1 for row in tickets if _ticket_sla_breached(row))

    finance_pending_entries = []
    for row in finance_rows:
        pending_value = row.get("pending_balance") if row.get("pending_balance") not in (None, "") else row.get("pending")
        try:
            pending_float = float(pending_value or 0)
        except Exception:
            pending_float = 0.0
        if pending_float > 0:
            finance_pending_entries.append((row, pending_float))

    finance_pending_rows = len(finance_pending_entries)
    finance_pending_delay_days = 0
    for row, _pending in finance_pending_entries:
        event_time = _event_time(row)
        if event_time is None:
            continue
        finance_pending_delay_days = max(finance_pending_delay_days, (current_time - event_time).days)

    sales_leads_count = len(leads)
    leads_open_or_active = sum(
        1
        for row in leads
        if str(row.get("raw_status") or "").strip().lower() not in {"closed", "cancelled", "canceled", "lost", "converted"}
    )
    bookings_count = len(bookings)
    success_bookings_count = sum(1 for row in bookings if str(row.get("booking_status") or "").strip().lower() == "success")
    travel_cart_attempts = len(travel_cart)
    site_visits_count = len(site_visits)
    site_visits_actual = sum(1 for row in site_visits if str(row.get("activity_type") or "").strip().lower() == "actual_staff_visit")
    site_visits_system = sum(1 for row in site_visits if str(row.get("activity_type") or "").strip().lower() == "system_scheduled")

    weak_connected_call_count = sum(1 for row in calls if _call_is_external(row) and _call_is_weak_connected(row))
    no_activity_flag = not any([calls, direct_whatsapp, tickets, finance_rows, leads, bookings, travel_cart, site_visits])

    customer_or_revenue_risk_flag = bool(
        unrecovered_missed_calls_count
        or whatsapp_response_gap_count
        or ticket_sla_breach_count
        or finance_pending_rows
        or (leads_open_or_active and success_bookings_count == 0 and role_scope in {"sales", "marketing", "onboarding"})
    )

    evidence_counts = compact_dict(
        {
            "events": counts.get("events"),
            "calls": counts.get("calls", len(calls)),
            "whatsapp": counts.get("whatsapp", len(whatsapp)),
            "tickets": counts.get("tickets", len(tickets)),
            "tickets_closed": counts.get("tickets_closed", counts.get("closed_tickets", len(tickets))),
            "tickets_open": counts.get("tickets_open"),
            "finance_rows": counts.get("finance_rows", len(finance_rows)),
            "leads": counts.get("leads", sales_leads_count),
            "bookings": counts.get("bookings", bookings_count),
            "success_bookings": counts.get("success_bookings", success_bookings_count),
            "travel_cart_attempts": counts.get("travel_cart_attempts", travel_cart_attempts),
            "site_visits": counts.get("site_visits", site_visits_count),
            "site_visits_actual": counts.get("site_visits_actual", site_visits_actual),
            "site_visits_system": counts.get("site_visits_system", site_visits_system),
        }
    )

    return compact_dict(
        {
            "missed_call_count": missed_call_count,
            "missed_inbound_working_hours_count": missed_inbound_working_hours_count,
            "recovered_missed_calls_count": recovered_missed_calls_count,
            "unrecovered_missed_calls_count": unrecovered_missed_calls_count,
            "after_hours_missed_calls_count": after_hours_missed_calls_count,
            "whatsapp_response_gap_count": whatsapp_response_gap_count,
            "no_activity_flag": no_activity_flag,
            "ticket_sla_breach_count": ticket_sla_breach_count,
            "open_ticket_count": None,
            "finance_pending_rows": finance_pending_rows,
            "finance_pending_delay_days": finance_pending_delay_days,
            "sales_leads_count": sales_leads_count,
            "leads_open_or_active": leads_open_or_active,
            "bookings_count": bookings_count,
            "success_bookings_count": success_bookings_count,
            "travel_cart_attempts": travel_cart_attempts,
            "site_visits": site_visits_count,
            "site_visits_actual": site_visits_actual,
            "site_visits_system": site_visits_system,
            "weak_connected_call_count": weak_connected_call_count,
            "customer_or_revenue_risk_flag": customer_or_revenue_risk_flag,
            "evidence_counts": evidence_counts,
        }
    )


def _manager_due_by(now: datetime, *, hours: int = 8) -> tuple[str, datetime]:
    same_day_cutoff = now.replace(hour=WORKDAY_END.hour, minute=WORKDAY_END.minute, second=0, microsecond=0)
    if now <= same_day_cutoff:
        return "Today before 6:30 PM", same_day_cutoff
    next_cutoff = next_working_day_end(now)
    return "Next working day before 6:30 PM", next_cutoff


def _priority_label(score: float) -> str:
    if score >= 8:
        return "High"
    if score >= 5:
        return "Medium"
    return "Low"


def _append_action(
    actions: list[dict[str, Any]],
    *,
    owner: str,
    action: str,
    priority_score: float,
    due_by: str,
    due_by_at: datetime,
    evidence: str,
) -> None:
    actions.append(
        compact_dict(
            {
                "owner": owner,
                "action": action,
                "priority": _priority_label(priority_score),
                "due_by": due_by,
                "due_by_at": due_by_at.isoformat(),
                "evidence": evidence,
                "status": "open",
            }
        )
    )


def build_heuristic_review(activity: dict[str, Any], signals: dict[str, Any], *, llm_requested: bool = False, now: Optional[datetime] = None) -> dict[str, Any]:
    current_time = now or now_ist_naive()
    role_scope = str(activity.get("role_scope") or "generic")
    staff = activity.get("staff") if isinstance(activity.get("staff"), dict) else {}
    counts = signals.get("evidence_counts") if isinstance(signals.get("evidence_counts"), dict) else {}

    score = 8.5
    priority_score = 2.0
    findings: list[str] = []
    coaching_points: list[str] = []
    data_gaps: list[str] = []
    actions: list[dict[str, Any]] = []

    if signals.get("no_activity_flag"):
        score = min(score, 4.0)
        priority_score = max(priority_score, 7.0)
        findings.append("No visible staff activity was found in the selected review window.")
        coaching_points.append("Confirm whether the current review window is too narrow or whether staff activity is not being captured in source systems.")

    unrecovered_missed = int(signals.get("unrecovered_missed_calls_count") or 0)
    if unrecovered_missed:
        score -= min(3.0, unrecovered_missed * 1.1)
        priority_score = max(priority_score, 9.0)
        findings.append(f"{unrecovered_missed} missed inbound customer call(s) did not show a recovery callback or message.")
        coaching_points.append("Missed inbound working-hour calls need same-day recovery through callback or WhatsApp.")
        due_by, due_at = _manager_due_by(current_time)
        _append_action(
            actions,
            owner=ACTION_OWNER_BY_ROLE.get(role_scope, ACTION_OWNER_BY_ROLE["generic"]),
            action="Review unrecovered missed calls and ensure customer callback or WhatsApp recovery happens immediately.",
            priority_score=9.0,
            due_by=due_by,
            due_by_at=due_at,
            evidence=f"Unrecovered missed calls: {unrecovered_missed}; working-hours missed calls: {signals.get('missed_inbound_working_hours_count', 0)}.",
        )

    whatsapp_gaps = int(signals.get("whatsapp_response_gap_count") or 0)
    if whatsapp_gaps:
        score -= min(2.0, whatsapp_gaps * 0.8)
        priority_score = max(priority_score, 8.0)
        findings.append(f"{whatsapp_gaps} customer WhatsApp conversation segment(s) show no visible staff reply within the expected working window.")
        coaching_points.append("Staff should close the loop on direct customer WhatsApp queries before end of business day.")
        due_by, due_at = _manager_due_by(current_time)
        _append_action(
            actions,
            owner=ACTION_OWNER_BY_ROLE.get(role_scope, ACTION_OWNER_BY_ROLE["generic"]),
            action="Check open customer WhatsApp queries and send a same-day response with the next step.",
            priority_score=8.0,
            due_by=due_by,
            due_by_at=due_at,
            evidence=f"WhatsApp response gaps: {whatsapp_gaps}.",
        )

    ticket_breaches = int(signals.get("ticket_sla_breach_count") or 0)
    if ticket_breaches:
        score -= min(2.0, ticket_breaches * 0.6)
        priority_score = max(priority_score, 7.0)
        findings.append(f"{ticket_breaches} ticket(s) appear to have exceeded the configured SLA threshold.")
        coaching_points.append("Escalate breached tickets earlier and keep closure evidence current.")
        due_by, due_at = _manager_due_by(current_time)
        _append_action(
            actions,
            owner=ACTION_OWNER_BY_ROLE.get(role_scope, ACTION_OWNER_BY_ROLE["generic"]),
            action="Review SLA-breached tickets and assign a closure owner with deadline tracking.",
            priority_score=7.0,
            due_by=due_by,
            due_by_at=due_at,
            evidence=f"SLA breaches detected: {ticket_breaches}. Open queue visibility is partial in current evidence.",
        )

    finance_pending_rows = int(signals.get("finance_pending_rows") or 0)
    if finance_pending_rows:
        finance_delay = int(signals.get("finance_pending_delay_days") or 0)
        score -= min(1.8, finance_pending_rows * 0.5)
        priority_score = max(priority_score, 8.0 if finance_delay >= 2 else 6.0)
        findings.append(f"{finance_pending_rows} finance/payment row(s) still show pending balance exposure.")
        coaching_points.append("Finance follow-up should make next-step ownership and due date explicit on pending rows.")
        due_by, due_at = _manager_due_by(current_time)
        _append_action(
            actions,
            owner="Finance Manager",
            action="Review pending payment rows, confirm customer follow-up, and clear aging finance items.",
            priority_score=8.0 if finance_delay >= 2 else 6.0,
            due_by=due_by,
            due_by_at=due_at,
            evidence=f"Pending finance rows: {finance_pending_rows}; max visible delay: {finance_delay} day(s).",
        )

    leads_open = int(signals.get("leads_open_or_active") or 0)
    success_bookings = int(signals.get("success_bookings_count") or 0)
    sales_leads = int(signals.get("sales_leads_count") or 0)
    travel_attempts = int(signals.get("travel_cart_attempts") or 0)
    if role_scope in {"sales", "marketing", "onboarding"} and leads_open:
        if success_bookings == 0:
            score -= 1.2
            priority_score = max(priority_score, 7.0)
            findings.append(f"{leads_open} lead(s) remain open/active with no visible successful booking in the review window.")
            due_by, due_at = _manager_due_by(current_time)
            _append_action(
                actions,
                owner=ACTION_OWNER_BY_ROLE.get(role_scope, "Sales Manager"),
                action="Review lead follow-up quality, confirm next step per open lead, and push site-visit or booking closure.",
                priority_score=7.0,
                due_by=due_by,
                due_by_at=due_at,
                evidence=f"Sales leads: {sales_leads}; open/active leads: {leads_open}; successful bookings: {success_bookings}; travel-cart attempts: {travel_attempts}.",
            )
        elif sales_leads > success_bookings:
            findings.append(f"Lead volume is higher than successful bookings, so conversion follow-up should be monitored.")

    site_visits = int(signals.get("site_visits") or 0)
    if role_scope in {"sales", "onboarding"} and sales_leads and site_visits == 0:
        priority_score = max(priority_score, 6.0)
        findings.append("Lead activity exists but there is no visible site-visit evidence in the same review window.")

    weak_calls = int(signals.get("weak_connected_call_count") or 0)
    if weak_calls:
        score -= min(1.0, weak_calls * 0.25)
        findings.append(f"{weak_calls} connected call(s) were under 30 seconds without clear outcome evidence.")
        coaching_points.append("Very short connected calls should still capture outcome or next-step context.")

    score = max(1.0, min(10.0, round(score, 1)))
    if not signals.get("customer_or_revenue_risk_flag"):
        priority_score = max(priority_score, 3.0 if not signals.get("no_activity_flag") else priority_score)
    priority_score = max(1.0, min(10.0, round(priority_score, 1)))

    risk = "Low"
    if signals.get("no_activity_flag") and not actions:
        risk = "No Data"
    elif priority_score >= 8:
        risk = "High"
    elif priority_score >= 5:
        risk = "Medium"

    if not llm_requested:
        data_gaps.append("LLM review was not run; this result uses deterministic manager-action heuristics only.")
    data_gaps.append("Current staff evidence does not expose open ticket queue state; ticket urgency is inferred from visible closed/resolved ticket history only.")
    data_gaps.append("Email activity is not part of the current staff activity evidence service.")

    customer_risk = ""
    if unrecovered_missed or whatsapp_gaps:
        customer_risk = f"{unrecovered_missed} unrecovered missed call(s) and {whatsapp_gaps} WhatsApp response gap(s) may leave customers waiting."

    revenue_risk = ""
    if finance_pending_rows:
        revenue_risk = f"{finance_pending_rows} pending finance row(s) may delay collections or closure."
    elif role_scope in {"sales", "marketing", "onboarding"} and leads_open and success_bookings == 0:
        revenue_risk = f"{leads_open} open lead(s) without visible conversion may reduce booking closure."

    operation_risk = ""
    if ticket_breaches:
        operation_risk = f"{ticket_breaches} SLA-breached ticket(s) may affect operational ownership and customer experience."
    elif signals.get("no_activity_flag"):
        operation_risk = "No visible activity in the review window makes operational coverage hard to confirm."

    trust_risk = ""
    if weak_calls or whatsapp_gaps:
        trust_risk = "Short or unanswered customer interactions weaken confidence in follow-up quality."

    if not actions:
        due_by, due_at = _manager_due_by(current_time)
        _append_action(
            actions,
            owner=ACTION_OWNER_BY_ROLE.get(role_scope, ACTION_OWNER_BY_ROLE["generic"]),
            action="No immediate action; monitor the next review window.",
            priority_score=priority_score,
            due_by=due_by,
            due_by_at=due_at,
            evidence="No high-confidence operational issue was detected in the current evidence window.",
        )

    reason_bits = []
    if unrecovered_missed:
        reason_bits.append(f"{unrecovered_missed} unrecovered missed call(s)")
    if whatsapp_gaps:
        reason_bits.append(f"{whatsapp_gaps} WhatsApp gap(s)")
    if ticket_breaches:
        reason_bits.append(f"{ticket_breaches} SLA breach(es)")
    if finance_pending_rows:
        reason_bits.append(f"{finance_pending_rows} pending finance row(s)")
    if not reason_bits:
        reason_bits.append("No high-confidence urgent gap detected in visible evidence")

    return compact_dict(
        {
            "staff": {
                "username": staff.get("username"),
                "team": staff.get("team"),
                "role_scope": role_scope,
            },
            "rating": {
                "overall_score": score,
                "priority_score": priority_score,
                "risk": risk,
                "reason": "; ".join(reason_bits) + ".",
            },
            "business_impact": compact_dict(
                {
                    "customer_risk": customer_risk,
                    "revenue_risk": revenue_risk,
                    "operation_risk": operation_risk,
                    "trust_risk": trust_risk,
                }
            ),
            "key_findings": findings or ["No high-confidence issue detected in the current visible evidence."],
            "recommended_actions": actions,
            "coaching_points": coaching_points or ["Continue monitoring communication quality and evidence capture in the next review window."],
            "data_gaps": list(dict.fromkeys(data_gaps)),
            "evidence_counts": counts,
        }
    )
