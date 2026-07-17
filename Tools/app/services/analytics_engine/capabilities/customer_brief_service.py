from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, time as dt_time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.analytics_engine.capabilities.timeline_access_service import (
    SCHEMA,
    TimelineAccessService,
    _now_local,
    _today_local,
    coerce_date,
    coerce_datetime,
    compact_inner_dict,
    summarize_text,
)


ACTIVE_TICKET_STATUSES = {"open", "in progress","in_progress","reopen", "reopened", "pending", "assigned", "new"}
BOOKING_BRIEF_VERSION = "customer_brief_booking:v4"
LLM_BRIEF_VERSION = "customer_brief_booking_llm:v5"

COMPANY_TO_CUSTOMER_DIRECTIONS = {"outgoing", "outbound", "sent", "reply", "from_admin", "dialed", "dial", "out"}
CUSTOMER_TO_COMPANY_DIRECTIONS = {"incoming", "inbound", "received", "from_customer", "missed", "receive", "in"}
NON_DIGIT_RE = re.compile(r"\D+")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
IST_OFFSET = timedelta(hours=5, minutes=30)
IST_TZ = timezone(IST_OFFSET)
EMAIL_SOURCE_TIMES_ARE_UTC = False
OFFICE_START_TIME = dt_time(9, 0)
OFFICE_END_TIME = dt_time(18, 30)
NEXT_MORNING_CALLBACK_DEADLINE = dt_time(10, 30)
MISSED_CALL_OFFICE_RECOVERY_WINDOW = timedelta(hours=2)


def _safe_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except Exception:
        try:
            return float(Decimal(str(value)))
        except Exception:
            return 0.0


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _unrestricted_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text_value = str(value)
    return text_value if text_value.strip() else None


def _max_dt(values: Sequence[Any]) -> Optional[datetime]:
    candidates = [coerce_datetime(v) for v in values if v not in (None, "")]
    candidates = [v for v in candidates if v is not None]
    return max(candidates) if candidates else None


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text_value = str(value).strip()
    if not text_value:
        return None
    try:
        return datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except Exception:
        return coerce_datetime(text_value)


def _to_ist_naive(value: Any, *, assume_naive_utc: bool = False) -> Optional[datetime]:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(IST_TZ).replace(tzinfo=None)
    if assume_naive_utc:
        return parsed + IST_OFFSET
    return parsed


def _coerce_id_value(value: Any) -> Any:
    if value in (None, ""):
        return None
    text_value = str(value).strip()
    if text_value.isdigit():
        try:
            return int(text_value)
        except Exception:
            return text_value
    return text_value


def _coerce_id_list(values: Sequence[Any]) -> List[Any]:
    seen: Set[str] = set()
    out: List[Any] = []
    for value in values or []:
        coerced = _coerce_id_value(value)
        if coerced in (None, ""):
            continue
        key = str(coerced)
        if key in seen:
            continue
        seen.add(key)
        out.append(coerced)
    return out


def _phone_last10(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    digits = NON_DIGIT_RE.sub("", str(value))
    return digits[-10:] if len(digits) >= 10 else None


def _phone_e164(value: Any) -> Optional[str]:
    last10 = _phone_last10(value)
    return f"91{last10}" if last10 else None


def _norm_email(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    match = EMAIL_RE.search(str(value).strip().lower())
    email = match.group(0) if match else str(value).strip().lower()
    return email if "@" in email else None


def _email_local_part(value: Any, *, fallback: str = "company") -> str:
    """Return a short debug label such as contact from contact@rentmystay.com."""
    if value in (None, ""):
        return fallback
    match = EMAIL_RE.search(str(value).strip().lower())
    if match:
        return match.group(0).split("@", 1)[0] or fallback
    text_value = str(value).strip().lower()
    if "@" in text_value:
        return text_value.split("@", 1)[0] or fallback
    return re.sub(r"[^a-z0-9_.+-]+", "", text_value)[:40] or fallback


def _safe_ident(value: str) -> str:
    if not SAFE_IDENT_RE.fullmatch(str(value or "")):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return str(value)


def _build_in_params(values: Sequence[Any], prefix: str) -> Tuple[str, Dict[str, Any]]:
    seen: Set[str] = set()
    deduped: List[Any] = []
    for value in values:
        if value in (None, ""):
            continue
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)

    if not deduped:
        return "(NULL)", {}

    params: Dict[str, Any] = {}
    holders: List[str] = []
    for idx, value in enumerate(deduped):
        key = f"{prefix}_{idx}"
        holders.append(f":{key}")
        params[key] = value
    return "(" + ", ".join(holders) + ")", params


def _phone_expr(column_name: str) -> str:
    column_name = _safe_ident(column_name)
    return f"RIGHT(REGEXP_REPLACE(COALESCE({column_name}::text, ''), '\\D', '', 'g'), 10)"

def _missed_call_sla_deadline(missed_at_value: Any) -> Optional[datetime]:
    missed_at = _to_ist_naive(missed_at_value)
    if missed_at is None:
        return None
    missed_time = missed_at.time()
    if OFFICE_START_TIME <= missed_time <= OFFICE_END_TIME:
        return missed_at + MISSED_CALL_OFFICE_RECOVERY_WINDOW

    if missed_time > OFFICE_END_TIME:
        next_day = missed_at.date() + timedelta(days=1)
        return datetime.combine(next_day, NEXT_MORNING_CALLBACK_DEADLINE)
    return datetime.combine(missed_at.date(), NEXT_MORNING_CALLBACK_DEADLINE)

def _normalize_missed_team(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    key = re.sub(r"[^a-z0-9]+", "", text.lower())

    if not key or key in {"customer", "company", "admin", "unknown", "none", "null", "na"}:
        return None
    if "ops" in key or "operation" in key:
        return "Ops"
    if "sales" in key:
        return "Sales"
    if "caretaker" in key:
        return "Caretaker"
    if "finance" in key:
        return "Finance"
    return text.title()

def _role_flow_parts(item: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    flow = str(item.get("role_flow") or "").strip()
    if "->" not in flow:
        return None, None
    left, right = flow.split("->", 1)
    return left.strip(), right.strip()


def _team_from_role_flow(item: Dict[str, Any], *, side: str) -> Optional[str]:
    left, right = _role_flow_parts(item)
    value = left if side == "source" else right
    team = _normalize_missed_team(value)
    if team:
        return team
    for fallback in (
        item.get("agent_role"),
        item.get("business_role"),
        item.get("agent_team"),
        item.get("role"),
    ):
        team = _normalize_missed_team(fallback)
        if team:
            return team
    return None

def _missed_call_team(item: Dict[str, Any]) -> Optional[str]:
    return _team_from_role_flow(item, side="target")

def _is_customer_to_team(item: Dict[str, Any]) -> bool:
    direction = _lower(item.get("direction"))
    flow = _lower(item.get("role_flow"))
    return direction in CUSTOMER_TO_COMPANY_DIRECTIONS or (
        flow.startswith("customer") and "->" in flow
    )

def _is_team_to_customer(item: Dict[str, Any]) -> bool:
    direction = _lower(item.get("direction"))
    flow = _lower(item.get("role_flow"))
    return direction in COMPANY_TO_CUSTOMER_DIRECTIONS or "-> customer" in flow

def _is_connected_call(item: Dict[str, Any]) -> bool:
    return _lower(item.get("status")) == "connected" or _safe_float(item.get("duration_sec")) > 0

def _same_customer_for_missed_sla(missed_call: Dict[str, Any], candidate: Dict[str, Any]) -> bool:
    missed_phone = _phone_last10(missed_call.get("customer_phone"))
    candidate_phone = _phone_last10(candidate.get("customer_phone"))
    if missed_phone and candidate_phone and missed_phone != candidate_phone:
        return False
    return True

def _is_customer_not_answered_call(item: Dict[str, Any]) -> bool:
    if _lower(item.get("channel")) != "call":
        return False
    if not _is_team_to_customer(item):
        return False
    if _is_connected_call(item):
        return False
    status = _lower(item.get("status"))
    direction = _lower(item.get("direction"))

    return (
        status in {
            "customer_not_answered",
            "not_answered",
            "no_answer",
            "not_connected",
            "rejected",
            "busy",
            "missed",
        }
        or direction in COMPANY_TO_CUSTOMER_DIRECTIONS
    )
    
def _missed_call_recovery_event(missed_call: Dict[str, Any], candidate: Dict[str, Any],) -> Optional[Dict[str, Any]]:
    missed_team = _missed_call_team(missed_call)
    if not missed_team:
        return None
    if not _same_customer_for_missed_sla(missed_call, candidate):
        return None
    channel = _lower(candidate.get("channel"))
    recovery_team: Optional[str] = None
    recovery_kind: Optional[str] = None
    customer_reached = False
    if channel == "call":
        if _is_team_to_customer(candidate):
            recovery_team = _team_from_role_flow(candidate, side="source")
            if _is_connected_call(candidate):
                recovery_kind = "same_team_callback_connected"
                customer_reached = True
            elif _is_customer_not_answered_call(candidate):
                recovery_kind = "same_team_callback_attempt_customer_not_answered"
                customer_reached = False
            else:
                return None
        elif _is_customer_to_team(candidate):
            if not _is_connected_call(candidate):
                return None
            recovery_team = _team_from_role_flow(candidate, side="target")
            recovery_kind = "customer_called_same_team_connected"
            customer_reached = True
        else:
            return None
    elif channel in {"whatsapp", "email"}:
        if not _is_team_to_customer(candidate):
            return None
        recovery_team = _team_from_role_flow(candidate, side="source")
        recovery_kind = f"same_team_{channel}_followup"
        customer_reached = True
    else:
        return None
    if recovery_team != missed_team:
        return None
    recovery_time = _to_ist_naive(candidate.get("time"))
    if recovery_time is None:
        return None

    return compact_inner_dict(
        {
            "time": recovery_time,
            "channel": candidate.get("channel"),
            "status": candidate.get("status"),
            "flow": candidate.get("role_flow"),
            "direction": candidate.get("direction"),
            "agent": candidate.get("agent"),
            "role": candidate.get("agent_role") or candidate.get("business_role"),
            "team": recovery_team,
            "kind": recovery_kind,
            "customer_reached": customer_reached,
        }
    )

def _annotate_missed_call_sla(messages: List[Dict[str, Any]]) -> None:
    ordered = sorted(messages or [], key=lambda item: item.get("time") or "")
    now_local = _now_local()

    for item in ordered:
        if item.get("channel") != "call":
            continue
        if _lower(item.get("status")) != "missed":
            continue
        missed_at = _to_ist_naive(item.get("time"))
        deadline = _missed_call_sla_deadline(item.get("time"))
        missed_team = _missed_call_team(item)

        if missed_at is None or deadline is None:
            item["missed_call_sla"] = {
                "status": "unknown",
                "reason": "missing_or_invalid_call_time",
                "missed_team": missed_team,
            }
            item["missed_call_recovered"] = False
            item["missed_call_sla_breached"] = False
            item["missed_call_owner_team"] = missed_team
            continue

        recovery_events: List[Dict[str, Any]] = []

        for candidate in ordered:
            candidate_time = _to_ist_naive(candidate.get("time"))
            if candidate_time is None or candidate_time <= missed_at:
                continue
            recovery_event = _missed_call_recovery_event(item, candidate)
            if not recovery_event:
                continue
            recovery_events.append(recovery_event)
        on_time_recovery = next(
            (row for row in recovery_events if row["time"] <= deadline),
            None,
        )
        first_recovery = recovery_events[0] if recovery_events else None

        if on_time_recovery:
            sla_status = "recovered_on_time"
            recovery = on_time_recovery
        elif first_recovery:
            sla_status = "breached_late_recovery"
            recovery = first_recovery
        elif now_local <= deadline:
            sla_status = "pending_before_deadline"
            recovery = None
        else:
            sla_status = "breached_no_recovery"
            recovery = None

        item["missed_call_sla"] = compact_inner_dict(
            {
                "status": sla_status,
                "office_hours": "09:00-18:30 IST",
                "rule": (
                    "within_2_hours"
                    if OFFICE_START_TIME <= missed_at.time() <= OFFICE_END_TIME
                    else "next_morning_before_10_30"
                ),
                "missed_team": missed_team,
                "missed_at": missed_at.isoformat(sep=" "),
                "deadline": deadline.isoformat(sep=" "),
                "recovered_at": recovery["time"].isoformat(sep=" ") if recovery else None,
                "recovery_team": recovery.get("team") if recovery else None,
                "recovery_channel": recovery.get("channel") if recovery else None,
                "recovery_status": recovery.get("status") if recovery else None,
                "recovery_flow": recovery.get("flow") if recovery else None,
                "recovery_direction": recovery.get("direction") if recovery else None,
                "recovery_agent": recovery.get("agent") if recovery else None,
                "recovery_kind": recovery.get("kind") if recovery else None,
            }
        )
        item["missed_call_recovered"] = sla_status == "recovered_on_time"
        item["missed_call_sla_breached"] = sla_status in {
            "breached_late_recovery",
            "breached_no_recovery",
        }
        item["missed_call_owner_team"] = missed_team
        item["missed_call_recovery_team"] = recovery.get("team") if recovery else None

def _real_early_cout_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    parsed = coerce_date(value)
    if parsed is None:
        text_value = str(value).strip()
        for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(text_value[:10], fmt).date()
                break
            except Exception:
                pass
    if parsed == date(1970, 1, 1):
        return None
    return parsed
        
@dataclass
class CustomerBriefService:
    """
    Booking-only customer brief builder.

    Input is intentionally limited to booking_id. The service no longer expands
    from lead_id/person_id/email/phone/user_id and no longer matches customer
    communication against admin/sales numbers. This keeps the brief scoped to
    direct booking evidence and prevents unrelated conversations from being
    pulled in through broad identity expansion.
    """

    db: Session
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        self.timeline = TimelineAccessService(self.db, self.schema)
        self._staff_role_cache: Dict[str, Dict[str, Any]] = {}
        self._line_role_cache: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Low-level SQL helpers
    # ------------------------------------------------------------------
    def _schema_ref(self) -> str:
        return f'"{_safe_ident(self.schema)}"'

    def _table_ref(self, table_name: str) -> str:
        return f"{self._schema_ref()}.{_safe_ident(table_name)}"

    def _rows(self, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        return [dict(row) for row in self.db.execute(text(sql), params or {}).mappings().fetchall()]

    def _select_list(self, table_name: str, columns: Sequence[str]) -> str:
        available = self.timeline.table_columns(table_name)
        parts: List[str] = []
        for column in columns:
            safe = _safe_ident(column)
            parts.append(safe if safe in available else f"NULL AS {safe}")
        return ",\n                       ".join(parts)

    def _coalesce_existing_columns(self, table_name: str, columns: Sequence[str]) -> str:
        """Return a COALESCE expression using only real table columns.

        _select_list() may emit `NULL AS missing_column` so every returned row
        has the same shape, but PostgreSQL cannot use that SELECT alias inside
        another expression in the same SELECT's ORDER BY. Therefore ORDER BY
        expressions must reference only columns that actually exist on the table.
        """
        available = self.timeline.table_columns(table_name)
        existing = [_safe_ident(column) for column in columns if _safe_ident(column) in available]
        if not existing:
            return "NULL"
        if len(existing) == 1:
            return existing[0]
        return f"COALESCE({', '.join(existing)})"

    def _order_by_recent_clause(self, table_name: str, columns: Sequence[str]) -> str:
        available = self.timeline.table_columns(table_name)
        time_expr = self._coalesce_existing_columns(table_name, columns)
        source_order = ", source_id DESC" if "source_id" in available else ""
        return f"ORDER BY {time_expr} DESC NULLS LAST{source_order}"
    
    def _fix_booking_state_using_early_cout(
        self,
        booking: dict[str, Any] | None,
        booking_row: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not booking or not booking_row:
            return booking

        status = str(
            booking_row.get("booking_status")
            or booking.get("booking_status")
            or booking.get("status")
            or ""
        ).strip().lower()
        if status != "success":
            return booking

        today = _today_local()
        travel_from = coerce_date(booking_row.get("travel_from_date") or booking.get("travel_from_date"))
        travel_to = coerce_date(booking_row.get("travel_to_date") or booking.get("travel_to_date"))
        early_cout = _real_early_cout_date(booking_row.get("early_cout"))
        fixed = dict(booking)
        quality_flags = [
            flag for flag in (fixed.get("quality_flags") or [])
            if flag != "actual_checkout_before_scheduled_end"
        ]
        if early_cout:
            fixed["is_active"] = False
            fixed["actual_checkout_date"] = early_cout.isoformat()
            fixed["actual_checkout_time"] = early_cout.isoformat()
            fixed["checkout_evidence_source"] = "staging_booking_confirm.early_cout"
            if travel_to and early_cout < travel_to:
                fixed["current_state"] = "checkout_completed_early"
                fixed["state_reason"] = f"early_cout={early_cout.isoformat()} before scheduled end {travel_to.isoformat()}"
                quality_flags.append("early_cout_before_scheduled_end")
            else:
                fixed["current_state"] = "checkout_completed"
                fixed["state_reason"] = f"early_cout={early_cout.isoformat()}"
        elif travel_from and travel_to and travel_from <= today <= travel_to:
            fixed["is_active"] = True
            fixed["current_state"] = "active_stay"
            fixed["state_reason"] = f"today {today.isoformat()} is within scheduled stay"
            fixed["actual_checkout_date"] = None
            fixed["actual_checkout_time"] = None
            fixed["checkout_evidence_source"] = None
        elif travel_to and travel_to < today:
            fixed["is_active"] = False
            fixed["current_state"] = "stay_completed"
            fixed["state_reason"] = f"scheduled stay ended on {travel_to.isoformat()}"
            fixed["actual_checkout_date"] = None
            fixed["actual_checkout_time"] = None
            fixed["checkout_evidence_source"] = None
        fixed["quality_flags"] = quality_flags
        return fixed

    # ------------------------------------------------------------------
    # Booking scope / directly related contacts
    # ------------------------------------------------------------------
    def _booking_rows_for_id(self, booking_id: int) -> List[Dict[str, Any]]:
        table_name = "staging_booking_confirm"
        if not self.timeline.table_exists(table_name):
            return []
        columns = [
            "source_id", "booking_id", "user_id", "lead_id", "prop_id",
            "booking_status", "host_confirm_status", "refund_status", "no_show_status",
            "booking_type", "period", "booking_datetime", "travel_from_date",
            "travel_to_date", "nights", "total_amount", "total_after_discount",
            "amount_paid", "advance_amount", "paid_advance_amount",
            "check_in_time", "check_out_time", "early_cout", "synced_at",
        ]
        select_list = self._select_list(table_name, columns)
        order_by = self._order_by_recent_clause(table_name, ["booking_datetime", "synced_at"])
        return self._rows(
            f"""
            SELECT {select_list}
            FROM {self._table_ref(table_name)}
            WHERE source_id::text = :booking_id_text
               OR booking_id::text = :booking_id_text
            {order_by}
            """,
            {"booking_id_text": str(booking_id)},
        )

    @staticmethod
    def _empty_scope(booking_id: int) -> Dict[str, Any]:
        return {
            "booking_ids": {str(booking_id)},
            "user_ids": set(),
            "lead_ids": set(),
            "property_ids": set(),
            "person_ids": set(),
            "phones": set(),
            "phone10s": set(),
            "emails": set(),
            "contacts": [],
            "contact_source_counts": Counter(),
            "_contact_seen": set(),
        }

    @staticmethod
    def _add_id(scope: Dict[str, Any], key: str, value: Any) -> None:
        if value in (None, "", 0, "0"):
            return
        scope[key].add(str(value))

    def _add_contact(
        self,
        scope: Dict[str, Any],
        *,
        kind: str,
        value: Any,
        source: str,
        label: Any = None,
        source_id: Any = None,
    ) -> None:
        if kind == "phone":
            normalized = _phone_e164(value)
            if not normalized:
                return
            scope["phones"].add(normalized)
            scope["phone10s"].add(normalized[-10:])
        elif kind == "email":
            normalized = _norm_email(value)
            if not normalized:
                return
            scope["emails"].add(normalized)
        else:
            return

        dedupe_key = (kind, normalized, source, str(source_id or ""))
        if dedupe_key not in scope["_contact_seen"]:
            scope["_contact_seen"].add(dedupe_key)
            scope["contacts"].append(
                compact_inner_dict(
                    {
                        "kind": kind,
                        "value": normalized,
                        "source": source,
                        "label": label,
                        "source_id": source_id,
                    }
                ) or {"kind": kind, "value": normalized, "source": source}
            )
        scope["contact_source_counts"][source] += 1

    def _add_user_account_contacts(self, scope: Dict[str, Any]) -> None:
        table_name = "staging_user_account"
        if not scope["user_ids"] or not self.timeline.table_exists(table_name):
            return
        columns = ["source_id", "username", "email", "phone_number", "normalized_phone"]
        select_list = self._select_list(table_name, columns)
        user_ids = sorted(scope["user_ids"])
        rows = self._rows(
            f"""
            SELECT {select_list}
            FROM {self._table_ref(table_name)}
            WHERE source_id::text = ANY(:user_ids)
            """,
            {"user_ids": [str(x) for x in user_ids]},
        )
        for row in rows:
            
            label = row.get("username")
            self._add_contact(scope, kind="email", value=row.get("email"), source="staging_user_account.email", label=label, source_id=row.get("source_id"))
            self._add_contact(scope, kind="phone", value=row.get("normalized_phone") or row.get("phone_number"), source="staging_user_account.phone", label=label, source_id=row.get("source_id"))

    def _add_booking_contact_info(self, scope: Dict[str, Any]) -> None:
        table_name = "staging_user_contact_info"
        if not self.timeline.table_exists(table_name):
            return
        columns = ["source_id", "user_id", "booking_id", "email", "contact_name", "mobile", "normalized_mobile", "added_by", "added_on", "synced_at"]
        select_list = self._select_list(table_name, columns)
        order_by = self._order_by_recent_clause(table_name, ["added_on", "synced_at"])
        booking_ids = sorted(scope["booking_ids"])
        rows = self._rows(
            f"""
            SELECT {select_list}
            FROM {self._table_ref(table_name)}
            WHERE booking_id::text = ANY(:booking_ids)
            {order_by}
            """,
            {"booking_ids": booking_ids},
        )
        for row in rows:
            self._add_id(scope, "user_ids", row.get("user_id"))
            label = row.get("contact_name")
            self._add_contact(scope, kind="email", value=row.get("email"), source="staging_user_contact_info.email", label=label, source_id=row.get("source_id"))
            self._add_contact(scope, kind="phone", value=row.get("normalized_mobile") or row.get("mobile"), source="staging_user_contact_info.mobile", label=label, source_id=row.get("source_id"))

    def _add_booking_form_contacts(self, scope: Dict[str, Any]) -> None:
        for table_name in ("staging_checkin_form", "staging_checkout_form"):
            if not self.timeline.table_exists(table_name):
                continue
            columns = ["source_id", "booking_id", "user_email", "added_on", "checkin_date", "checkout_date", "synced_at"]
            select_list = self._select_list(table_name, columns)
            order_by = self._order_by_recent_clause(table_name, ["added_on", "checkin_date", "checkout_date", "synced_at"])
            rows = self._rows(
                f"""
                SELECT {select_list}
                FROM {self._table_ref(table_name)}
                WHERE booking_id::text = ANY(:booking_ids)
                {order_by}
                """,
                {"booking_ids": sorted(scope["booking_ids"])},
            )
            for row in rows:
                self._add_contact(scope, kind="email", value=row.get("user_email"), source=f"{table_name}.user_email", source_id=row.get("source_id"))

    def _linked_lead_rows_for_booking(self, scope: Dict[str, Any]) -> List[Dict[str, Any]]:
        table_name = "staging_lead_tracking"
        if not self.timeline.table_exists(table_name):
            return []
        columns = [
            "source_id", "user_id", "booking_id", "executive_id", "created_at", "closed_at",
            "raw_status", "contact_number", "contact_number_alt", "email", "assigned_to", "synced_at",
        ]
        select_list = self._select_list(table_name, columns)
        conds: List[str] = []
        params: Dict[str, Any] = {}
        if scope["booking_ids"]:
            conds.append("booking_id::text = ANY(:booking_ids)")
            params["booking_ids"] = sorted(scope["booking_ids"])
        if scope["lead_ids"]:
            conds.append("source_id::text = ANY(:lead_ids)")
            params["lead_ids"] = sorted(scope["lead_ids"])
        if not conds:
            return []
        order_by = self._order_by_recent_clause(table_name, ["created_at", "synced_at"])
        rows = self._rows(
            f"""
            SELECT {select_list}
            FROM {self._table_ref(table_name)}
            WHERE {' OR '.join(f'({cond})' for cond in conds)}
            {order_by}
            """,
            params,
        )
        for row in rows:
            self._add_id(scope, "lead_ids", row.get("source_id"))
            self._add_id(scope, "user_ids", row.get("user_id"))
            label = row.get("assigned_to") or row.get("executive_id")
            self._add_contact(
                scope,
                kind="phone",
                value=row.get("contact_number"),
                source="staging_lead_tracking.booking_linked.contact_number",
                label=label,
                source_id=row.get("source_id"),
            )
            self._add_contact(
                scope,
                kind="phone",
                value=row.get("contact_number_alt"),
                source="staging_lead_tracking.booking_linked.contact_number_alt",
                label=label,
                source_id=row.get("source_id"),
            )
            self._add_contact(
                scope,
                kind="email",
                value=row.get("email"),
                source="staging_lead_tracking.booking_linked.email",
                label=label,
                source_id=row.get("source_id"),
            )
        return rows

    def _build_booking_scope(self, booking_id: int, booking_rows: List[Dict[str, Any]]) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        scope = self._empty_scope(booking_id)

        for row in booking_rows:
            self._add_id(scope, "booking_ids", row.get("source_id"))
            self._add_id(scope, "booking_ids", row.get("booking_id"))
            self._add_id(scope, "user_ids", row.get("user_id"))
            self._add_id(scope, "lead_ids", row.get("lead_id"))
            self._add_id(scope, "property_ids", row.get("prop_id"))

        self._add_booking_contact_info(scope)
        self._add_user_account_contacts(scope)
        self._add_booking_form_contacts(scope)
        linked_lead_rows = self._linked_lead_rows_for_booking(scope)

        # If contact_info introduced additional user_ids, pull their direct account
        # contact once more. This is still booking-scoped because the user_id came
        # from a booking_id row in staging_user_contact_info.
        self._add_user_account_contacts(scope)
        return scope, linked_lead_rows

    @staticmethod
    def _public_scope(scope: Dict[str, Any]) -> Dict[str, Any]:
        source_counts = dict(scope.get("contact_source_counts") or {})
        return compact_inner_dict(
            {
                "booking_ids": sorted(scope.get("booking_ids") or []),
                "user_ids": [int(x) if str(x).isdigit() else x for x in sorted(scope.get("user_ids") or [])],
                "lead_ids": [int(x) if str(x).isdigit() else x for x in sorted(scope.get("lead_ids") or [])],
                "property_ids": [int(x) if str(x).isdigit() else x for x in sorted(scope.get("property_ids") or [])],
                "person_ids": [int(x) if str(x).isdigit() else x for x in sorted(scope.get("person_ids") or [])],
                "phones": sorted(scope.get("phones") or []),
                "emails": sorted(scope.get("emails") or []),
                "contact_sources": scope.get("contacts") or [],
                "contact_source_counts": source_counts,
            }
        ) or {}

    # ------------------------------------------------------------------
    # Tickets / support
    # ------------------------------------------------------------------
    def _ticket_rows_for_booking(self, universe: Dict[str, Any]) -> List[Dict[str, Any]]:
        table_name = "staging_user_ticket"
        if not self.timeline.table_exists(table_name):
            return []
        booking_ids = [str(x) for x in (universe.get("booking_ids") or []) if x not in (None, "")]
        if not booking_ids:
            return []
        columns = [
            "source_id", "booking_id", "prop_id", "building_id", "building_name",
            "category", "priority", "description", "mobile_number", "unit_number",
            "status", "reopen_flag", "created_at", "assigned_to", "resolved_by",
            "closed_by", "close_date", "active_days", "synced_at",
        ]
        select_list = self._select_list(table_name, columns)
        order_by = self._order_by_recent_clause(table_name, ["close_date", "created_at", "synced_at"])
        return self._rows(
            f"""
            SELECT {select_list}
            FROM {self._table_ref(table_name)}
            WHERE booking_id::text = ANY(:booking_ids)
            {order_by}
            """,
            {"booking_ids": booking_ids},
        )

    def _build_support(self, ticket_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        active_rows = [row for row in ticket_rows if _lower(row.get("status")) in ACTIVE_TICKET_STATUSES]
        closed_rows = [row for row in ticket_rows if _lower(row.get("status")) not in ACTIVE_TICKET_STATUSES]
        def _format_ticket_with_team(row: dict[str, Any]) -> dict[str, Any]:
            ticket = self.timeline._format_ticket(row) or {}
            ticket["ticket_id"] = row.get("source_id")
            if row.get("category") not in (None, ""):
                ticket["category"] = row.get("category")
            if row.get("assigned_to") not in (None, ""):
                ticket["assigned_to"] = row.get("assigned_to")
            if row.get("resolved_by") not in (None, ""):
                ticket["resolved_by"] = row.get("resolved_by")
            if row.get("closed_by") not in (None, ""):
                ticket["closed_by"] = row.get("closed_by")
            return compact_inner_dict(ticket) or ticket
        formatted_open_tickets = [_format_ticket_with_team(row) for row in active_rows]
        formatted_closed_tickets = [_format_ticket_with_team(row) for row in closed_rows]
        freshness = _max_dt([row.get("synced_at") or row.get("close_date") or row.get("created_at") for row in ticket_rows])
        return compact_inner_dict(
            {
                "total_ticket_count": len(ticket_rows),
                "open_ticket_count": len(active_rows),
                "closed_ticket_count": len(closed_rows),
                "open_tickets": [ticket for ticket in formatted_open_tickets if ticket],
                "closed_tickets": [ticket for ticket in formatted_closed_tickets if ticket],
                "freshness": compact_inner_dict({"last_updated_at": freshness}),
                "quality_flags": [],
            }
        ) or {"total_ticket_count": 0, "open_ticket_count": 0, "closed_ticket_count": 0}

    # ------------------------------------------------------------------
    # Communication fetchers. Booking contacts only; no lead/admin/sales matching.
    # ------------------------------------------------------------------
    def _window_from_days(self, days: int) -> tuple[datetime, datetime]:
        safe_days = max(1, int(days or 30))
        end_dt = _now_local()
        return end_dt - timedelta(days=safe_days), end_dt

    @staticmethod
    def _role_label_from_team(team: Any, *, fallback: str = "company") -> str:
        text_value = " ".join(str(team or "").strip().lower().replace("_", " ").split())
        if not text_value or text_value in {"0", "none", "null", "na", "n/a"}:
            return fallback
        if "care" in text_value:
            return "caretaker"
        if "sales" in text_value:
            return "sales"
        if "finance" in text_value or "fin" == text_value:
            return "finance"
        if "ops" in text_value or "operation" in text_value:
            return "ops"
        if "admin" in text_value:
            return "admin"
        if "support" in text_value:
            return "support"
        return text_value[:40]

    @staticmethod
    def _flow_from_direction(direction: Any, business_role: str = "company") -> str:
        normalized = _lower(direction)
        role = business_role or "company"
        if normalized in COMPANY_TO_CUSTOMER_DIRECTIONS:
            return f"{role} -> customer"
        if normalized in CUSTOMER_TO_COMPANY_DIRECTIONS:
            return f"customer -> {role}"
        return f"customer ↔ {role}" if role != "company" else "unknown"

    def _staff_role_from_account(self, staff_ref: Any) -> Dict[str, Any]:
        ref = str(staff_ref or "").strip()
        if not ref or not self.timeline.table_exists("staging_user_account"):
            return {}
        cache_key = ref.lower()
        if cache_key in self._staff_role_cache:
            return dict(self._staff_role_cache[cache_key])

        row = self._rows(
            f'''
            SELECT source_id, username, team, is_admin
            FROM {self._table_ref("staging_user_account")}
            WHERE LOWER(TRIM(COALESCE(username::text, ''))) = :ref
               OR source_id::text = :ref_raw
            ORDER BY source_id DESC
            LIMIT 1
            ''',
            {"ref": cache_key, "ref_raw": ref},
        )
        data = row[0] if row else {}
        team = data.get("team")
        role = self._role_label_from_team(team, fallback="company")
        result = compact_inner_dict(
            {
                "role": role,
                "team": team,
                "actor": data.get("username") or ref,
                "source": "staging_user_account",
            }
        ) or {}
        self._staff_role_cache[cache_key] = dict(result)
        return result

    def _staff_role_from_line(self, sales_phone: Any) -> Dict[str, Any]:
        phone10 = _phone_last10(sales_phone)
        if not phone10 or not self.timeline.table_exists("staging_staff_phone_assignment"):
            return {}
        if phone10 in self._line_role_cache:
            return dict(self._line_role_cache[phone10])

        row = self._rows(
            f'''
            SELECT phone10, normalized_phone, tag_to, username, team
            FROM {self._table_ref("staging_staff_phone_assignment")}
            WHERE phone10 = :phone10
            LIMIT 1
            ''',
            {"phone10": phone10},
        )
        data = row[0] if row else {}
        team = data.get("team")
        role = self._role_label_from_team(team, fallback="company")
        result = compact_inner_dict(
            {
                "role": role,
                "team": team,
                "line_owner": data.get("username") or data.get("tag_to"),
                "line_phone": data.get("normalized_phone"),
                "source": "staging_staff_phone_assignment",
            }
        ) or {}
        self._line_role_cache[phone10] = dict(result)
        return result

    def _call_business_party(self, row: Dict[str, Any]) -> Dict[str, Any]:
        staff_ref = row.get("executive_id") or row.get("executive_name")
        account = self._staff_role_from_account(staff_ref)
        line = self._staff_role_from_line(row.get("sales_phone"))
        role = account.get("role") if account.get("role") != "company" else None
        role = role or (line.get("role") if line.get("role") != "company" else None) or "company"
        actor = account.get("actor") or row.get("executive_id") or row.get("executive_name") or line.get("line_owner")
        return compact_inner_dict(
            {
                "role": role,
                "actor": actor,
                "team": account.get("team") or line.get("team"),
                "line_owner": line.get("line_owner"),
                "line_phone": line.get("line_phone") or _phone_e164(row.get("sales_phone")),
                "role_source": account.get("source") or line.get("source"),
            }
        ) or {"role": role}

    def _fetch_whatsapp_messages(self, scope: Dict[str, Any], start_dt: datetime, end_dt: datetime, *, unrestricted: bool = False) -> List[Dict[str, Any]]:
        table_name = "staging_whatsapp_messages"
        if not scope.get("phone10s") or not self.timeline.table_exists(table_name):
            return []
        columns_available = self.timeline.table_columns(table_name)
        if "cx_number" not in columns_available or "message_time" not in columns_available:
            return []
        columns = ["source_id", "message_time", "direction", "message_type", "clean_content", "admin_number", "cx_number", "remote_jid"]
        select_list = self._select_list(table_name, columns)
        rows = self._rows(
            f"""
            SELECT {select_list}
            FROM {self._table_ref(table_name)}
            WHERE {_phone_expr('cx_number')} = ANY(:phones)
              AND message_time >= :start_dt
              AND message_time < :end_dt
            ORDER BY message_time ASC NULLS LAST, source_id ASC
            """,
            {"phones": sorted(scope["phone10s"]), "start_dt": start_dt, "end_dt": end_dt},
        )
        out: List[Dict[str, Any]] = []
        for row in rows:
            business_party = self._staff_role_from_line(row.get("admin_number"))
            business_role = business_party.get("role") if business_party.get("role") != "company" else None
            business_role = business_role or "company"
            out.append(
                compact_inner_dict(
                    {
                        "time": row.get("message_time"),
                        "role_flow": self._flow_from_direction(row.get("direction"), business_role),
                        "direction": row.get("direction"),
                        "message_type": row.get("message_type"),
                        "customer_phone": _phone_e164(row.get("cx_number")),
                        "business_phone": _phone_e164(row.get("admin_number")),
                        "business_role": business_role,
                        "agent": business_party.get("line_owner"),
                        "agent_team": business_party.get("team"),
                        "text": _unrestricted_text(row.get("clean_content")) if unrestricted else summarize_text(row.get("clean_content"), 600),
                    }
                ) or {}
            )
        return out

    def _fetch_call_messages(self, scope: Dict[str, Any], start_dt: datetime, end_dt: datetime, *, unrestricted: bool = False) -> List[Dict[str, Any]]:
        table_name = "staging_call_log_unified"
        if not scope.get("phone10s") or not self.timeline.table_exists(table_name):
            return []
        columns_available = self.timeline.table_columns(table_name)
        if "counterparty_phone" not in columns_available or "call_time" not in columns_available:
            return []
        columns = [
            "source_id", "executive_id", "executive_name", "call_time", "talk_time_sec",
            "call_direction", "call_result", "counterparty_phone", "sales_phone",
            "translated_text", "transcript_text", "transcript_text_eleven_labs", "raw_transcripts",
            "intent", "emotion", "tone", "action_layer", "context", "outcome", "language", "audio_url",
        ]
        select_list = self._select_list(table_name, columns)
        rows = self._rows(
            f"""
            SELECT {select_list}
            FROM {self._table_ref(table_name)}
            WHERE {_phone_expr('counterparty_phone')} = ANY(:phones)
              AND call_time >= :start_dt
              AND call_time < :end_dt
            ORDER BY call_time ASC NULLS LAST, source_id ASC
            """,
            {"phones": sorted(scope["phone10s"]), "start_dt": start_dt, "end_dt": end_dt},
        )
        out: List[Dict[str, Any]] = []
        for row in rows:
            direction = _lower(row.get("call_direction")) or "unknown"
            duration = int(_safe_float(row.get("talk_time_sec"))) if row.get("talk_time_sec") not in (None, "") else 0
            raw_status = _lower(row.get("call_result"))
            if raw_status in {"connected", "answered", "completed"} or duration > 0:
                status = "connected"
            elif direction in COMPANY_TO_CUSTOMER_DIRECTIONS:
                status = "customer_not_answered"
            elif direction in CUSTOMER_TO_COMPANY_DIRECTIONS:
                status = "missed"
            else:
                status = raw_status if raw_status and raw_status != "missed" else "unknown"
            raw_transcript = (
                row.get("translated_text")
                or row.get("transcript_text")
                or row.get("transcript_text_eleven_labs")
                or row.get("raw_transcripts")
            )
            transcript = (
                (_unrestricted_text(raw_transcript) or "no transcript")
                if unrestricted
                else summarize_text(raw_transcript, 600)
            )
            business_party = self._call_business_party(row)
            business_role = business_party.get("role") or "company"
            out.append(
                compact_inner_dict(
                    {
                        "time": row.get("call_time"),
                        "role_flow": self._flow_from_direction(direction, business_role),
                        "event_type": "call",
                        "status": status,
                        "direction": direction,
                        "duration_sec": duration,
                        "agent": business_party.get("actor") or row.get("executive_id") or row.get("executive_name"),
                        "agent_role": business_role,
                        "agent_team": business_party.get("team"),
                        "line_owner": business_party.get("line_owner"),
                        "customer_phone": _phone_e164(row.get("counterparty_phone")),
                        "business_phone": _phone_e164(row.get("sales_phone")),
                        "text": transcript,
                        "intent": row.get("intent"),
                        "emotion": row.get("emotion"),
                        "tone": row.get("tone"),
                        "action_layer": row.get("action_layer"),
                        "context": row.get("context"),
                        "outcome": row.get("outcome"),
                        "language": row.get("language"),
                        "audio_url": row.get("audio_url"),
                    }
                ) or {}
            )
        return out

    def _email_receiver_label(self, receiver: Optional[str], customer_emails: Sequence[str]) -> str:
        receiver_l = str(receiver or "").lower()
        customer_set = {_norm_email(email) for email in customer_emails if _norm_email(email)}
        if any(email and email in receiver_l for email in customer_set):
            return "customer"
        return _email_local_part(receiver, fallback="company")

    def _infer_email_role_flow(self, sender: Optional[str], receiver: Optional[str], customer_emails: Sequence[str]) -> str:
        sender_norm = _norm_email(sender)
        customer_set = {_norm_email(email) for email in customer_emails if _norm_email(email)}
        sender_label = "customer" if sender_norm in customer_set else _email_local_part(sender, fallback="company")
        receiver_label = self._email_receiver_label(receiver, customer_emails)
        return f"{sender_label} -> {receiver_label}" if sender_label or receiver_label else "unknown"

    @staticmethod
    def _is_invoice_email(subject: Any, text_value: Any) -> bool:
        subject_text = " ".join(str(subject or "").strip().lower().split())
        body_text = " ".join(str(text_value or "").strip().lower().split())
        if any(token in subject_text for token in ("invoice", "receipt", "payment updated", "amount updated")):
            return True
        return any(
            marker in body_text
            for marker in (
                "your booking invoice has been updated",
                "download the required receipt",
                "paid and pending amount",
            )
        )

    def _fetch_email_messages(self, scope: Dict[str, Any], start_dt: datetime, end_dt: datetime, *, unrestricted: bool = False) -> List[Dict[str, Any]]:
        table_name = "staging_email_messages"
        emails = sorted(scope.get("emails") or [])
        if not emails or not self.timeline.table_exists(table_name):
            return []
        columns_available = self.timeline.table_columns(table_name)
        if "email_date" not in columns_available:
            return []
        columns = ["source_id", "msgid", "thread_id", "direction", "email_date", "sender", "receiver", "subject", "body", "snippet"]
        select_list = self._select_list(table_name, columns)
        in_sql, in_params = _build_in_params(emails, "email")
        like_params = {f"email_like_{idx}": f"%{email}%" for idx, email in enumerate(emails)}
        receiver_match = " OR ".join(f"LOWER(COALESCE(receiver, '')) LIKE :email_like_{idx}" for idx in range(len(emails)))
        db_start_dt = start_dt - IST_OFFSET if EMAIL_SOURCE_TIMES_ARE_UTC else start_dt
        db_end_dt = end_dt - IST_OFFSET if EMAIL_SOURCE_TIMES_ARE_UTC else end_dt
        rows = self._rows(
            f"""
            WITH matched AS (
                SELECT {select_list}
                FROM {self._table_ref(table_name)}
                WHERE (
                        LOWER(COALESCE(sender, '')) IN {in_sql}
                     OR ({receiver_match})
                )
                  AND email_date >= :start_dt
                  AND email_date < :end_dt
            ),
            keyed AS (
                SELECT
                    *,
                    LOWER(TRIM(COALESCE(sender::text, ''))) AS sender_norm,
                    LOWER(TRIM(COALESCE(subject::text, ''))) AS subject_norm,
                    LOWER(TRIM(COALESCE(direction::text, ''))) AS direction_norm,
                    MD5(
                        LOWER(
                            REGEXP_REPLACE(
                                COALESCE(NULLIF(body::text, ''), NULLIF(snippet::text, ''), subject::text, ''),
                                '\\s+',
                                ' ',
                                'g'
                            )
                        )
                    ) AS content_hash,
                    (
                        DATE_TRUNC('minute', email_date)
                        - ((EXTRACT(minute FROM email_date)::int % 5) * INTERVAL '1 minute')
                    ) AS email_bucket_5m,
                    DATE_TRUNC('second', email_date) AS email_second
                FROM matched
            ),
            grouped AS (
                SELECT
                    sender_norm,
                    subject_norm,
                    content_hash,
                    email_bucket_5m,
                    COUNT(*) AS duplicate_count,
                    MIN(email_date) AS first_email_date,
                    MIN(email_second) AS first_email_second,
                    MAX(email_second) AS last_email_second,
                    COUNT(DISTINCT direction_norm) AS direction_variants,
                    ARRAY_AGG(source_id::text ORDER BY email_date NULLS LAST, source_id ASC) AS duplicate_source_ids
                FROM keyed
                GROUP BY sender_norm, subject_norm, content_hash, email_bucket_5m
            ),
            ranked AS (
                SELECT
                    k.*,
                    g.duplicate_count,
                    g.first_email_date,
                    g.first_email_second,
                    g.last_email_second,
                    g.direction_variants,
                    g.duplicate_source_ids,
                    ROW_NUMBER() OVER (
                        PARTITION BY k.sender_norm, k.subject_norm, k.content_hash, k.email_bucket_5m
                        ORDER BY
                            CASE WHEN k.direction_norm IN ('outgoing', 'outbound', 'sent', 'reply', 'from_admin') THEN 0 ELSE 1 END,
                            k.email_date ASC NULLS LAST,
                            k.source_id ASC
                    ) AS rn
                FROM keyed k
                JOIN grouped g
                  ON g.sender_norm = k.sender_norm
                 AND g.subject_norm = k.subject_norm
                 AND g.content_hash = k.content_hash
                 AND g.email_bucket_5m IS NOT DISTINCT FROM k.email_bucket_5m
            )
            SELECT
                source_id,
                msgid,
                thread_id,
                direction,
                CASE
                    WHEN duplicate_count > 1
                     AND (first_email_second = last_email_second OR direction_variants > 1)
                    THEN first_email_date
                    ELSE email_date
                END AS email_date,
                sender,
                receiver,
                subject,
                body,
                snippet,
                duplicate_count,
                duplicate_source_ids
            FROM ranked
            WHERE rn = 1
               OR NOT (
                    duplicate_count > 1
                    AND (first_email_second = last_email_second OR direction_variants > 1)
               )
            ORDER BY email_date ASC NULLS LAST, source_id ASC
            """,
            {**in_params, **like_params, "start_dt": db_start_dt, "end_dt": db_end_dt},
        )
        out: List[Dict[str, Any]] = []
        for row in rows:
            message_text = (
                (row.get("body") or row.get("snippet") or row.get("subject"))
                if unrestricted
                else (row.get("snippet") or row.get("body") or row.get("subject"))
            )
            if self._is_invoice_email(row.get("subject"), message_text):
                continue
            out.append(
                compact_inner_dict(
                    {
                        "time": _to_ist_naive(row.get("email_date"), assume_naive_utc=EMAIL_SOURCE_TIMES_ARE_UTC),
                        "role_flow": self._infer_email_role_flow(row.get("sender"), row.get("receiver"), emails),
                        "sender": _email_local_part(row.get("sender"), fallback="company"),
                        "receiver": self._email_receiver_label(row.get("receiver"), emails),
                        "subject": row.get("subject"),
                        "text": _unrestricted_text(message_text) if unrestricted else summarize_text(message_text, 600),
                    }
                ) or {}
            )
        return out

    def _conversation_for_booking(self, scope: Dict[str, Any], days: int = 30, *, unrestricted: bool = False) -> Dict[str, Any]:
        safe_days = max(1, int(days or 30))
        start_dt, end_dt = self._window_from_days(safe_days)
        sections = {
            "call": self._fetch_call_messages(scope, start_dt, end_dt, unrestricted=unrestricted),
            "whatsapp": self._fetch_whatsapp_messages(scope, start_dt, end_dt, unrestricted=unrestricted),
            "email": self._fetch_email_messages(scope, start_dt, end_dt, unrestricted=unrestricted),
        }
        all_messages: List[Dict[str, Any]] = []
        for channel_name, messages in sections.items():
            for item in messages:
                merged = dict(item)
                merged["channel"] = channel_name
                all_messages.append(merged)
        all_messages.sort(key=lambda item: item.get("time") or "")
        _annotate_missed_call_sla(all_messages)
        quality_flags: List[str] = []
        if not scope.get("phone10s") and not scope.get("emails"):
            quality_flags.append("no_booking_contacts_available_for_conversation_lookup")
        if not unrestricted and any(str(item.get("text") or "").endswith("...") for item in all_messages):
            quality_flags.append("contains_truncated_message_text")
        return {
            "channel": "any",
            "days": safe_days,
            "window": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
            "call": {"messages": sections["call"]},
            "whatsapp": {"messages": sections["whatsapp"]},
            "email": {"messages": sections["email"]},
            "recent_message_count": len(all_messages),
            "recent_messages": all_messages,
            "freshness": compact_inner_dict({"generated_at": _now_local(), "last_message_at": _max_dt([item.get("time") for item in all_messages])}) or {},
            "quality_flags": quality_flags,
        }

    def _normalize_conversation(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        recent_messages = sorted(payload.get("recent_messages") or [], key=lambda item: item.get("time") or "", reverse=True)
        return {
            "window": payload.get("window"),
            "recent_message_count": len(recent_messages),
            "recent_messages": recent_messages,
            "freshness": payload.get("freshness") or {},
            "quality_flags": payload.get("quality_flags") or [],
        }

    # ------------------------------------------------------------------
    # Compact LLM output
    # ------------------------------------------------------------------
    @staticmethod
    def _date_label(value: Any, *, with_time: bool = False) -> str | None:
        dt = coerce_datetime(value)
        if not dt:
            return None
        return dt.strftime("%Y-%m-%d %H:%M") if with_time else dt.strftime("%Y-%m-%d")

    @staticmethod
    def _period_label(start_value: Any, end_value: Any) -> str | None:
        start = CustomerBriefService._date_label(start_value)
        end = CustomerBriefService._date_label(end_value)
        if start and end:
            return f"{start}..{end}"
        return start or end

    @staticmethod
    def _dedupe_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        out: List[Dict[str, Any]] = []
        for item in messages or []:
            channel = str(item.get("channel") or "").lower()
            if channel == "call":
                key = (
                    item.get("time"), "call", item.get("role_flow"), item.get("direction"), item.get("status"),
                    int(_safe_float(item.get("duration_sec"))) if item.get("duration_sec") not in (None, "") else None,
                    item.get("customer_phone"),
                )
            else:
                key = (
                    item.get("time"), channel, item.get("role_flow"),
                    summarize_text(item.get("subject"), 120), summarize_text(item.get("text"), 160),
                )
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def _compact_booking_for_llm(self, booking: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not booking:
            return None
        return compact_inner_dict(
            {
                "id": booking.get("booking_id"),
                "status": booking.get("status") or booking.get("booking_status"),
                "state": booking.get("current_state"),
                "active": booking.get("is_active"),
                "property_id": booking.get("property_id"),
                "type": booking.get("type") or booking.get("booking_type"),
                "stay": self._period_label(booking.get("travel_from_date"), booking.get("travel_to_date")),
                "days_until_booked_end": booking.get("days_until_booked_end"),
                "actual_checkout": self._date_label(booking.get("actual_checkout_date")),
                "state_reason": booking.get("state_reason"),
            }
        )

    def _compact_support_for_llm(self, support: Dict[str, Any]) -> Dict[str, Any]:
        def compact_ticket(ticket: Dict[str, Any]) -> Dict[str, Any]:
            ticket_id = ticket.get("ticket_id") or ticket.get("source_id") or ticket.get("id")
            return compact_inner_dict(
                {
                    "ticket_id": ticket_id,
                    "category": ticket.get("category"),
                    "status": ticket.get("status"),
                    "priority": ticket.get("priority"),
                    "created_at": self._date_label(ticket.get("created_at"), with_time=True),
                    "closed_at": self._date_label(ticket.get("close_date"), with_time=True),
                    "age_days": ticket.get("active_days"),
                    "assigned_to": ticket.get("assigned_to"),
                    "text": summarize_text(ticket.get("description"), 140),
                }
            ) or {}

        open_tickets = [ticket for ticket in (compact_ticket(row) for row in (support.get("open_tickets") or [])) if ticket]
        closed_tickets = [ticket for ticket in (compact_ticket(row) for row in (support.get("closed_tickets") or [])) if ticket]
        visible_closed = closed_tickets[:8]
        return compact_inner_dict(
            {
                "total_ticket_count": support.get("total_ticket_count", 0),
                "open_ticket_count": support.get("open_ticket_count", 0),
                "closed_ticket_count": support.get("closed_ticket_count", len(closed_tickets)),
                "open_tickets": open_tickets[:8],
                "closed_tickets": visible_closed,
                "truncated": len(open_tickets) > 8 or len(closed_tickets) > 8,
                "flags": support.get("quality_flags") or [],
            }
        ) or {"open_ticket_count": 0, "total_ticket_count": 0}

    def _conversation_stats_for_llm(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        channel_counts = Counter(str(item.get("channel") or "unknown") for item in messages)
        flow_counts = Counter(str(item.get("role_flow") or "unknown") for item in messages)
        connected_calls = 0
        missed_calls = 0
        missed_call_recovered = 0
        missed_call_sla_breached = 0
        missed_call_sla_pending = 0
        missed_calls_by_team: Counter[str] = Counter()
        missed_sla_breached_by_team: Counter[str] = Counter()
        missed_recovered_by_team: Counter[str] = Counter()
        customer_not_answered_calls = 0
        call_seconds = 0
        agents = Counter()
        for item in messages:
            if item.get("channel") != "call":
                continue
            status = _lower(item.get("status"))
            if status == "connected":
                connected_calls += 1
                call_seconds += int(_safe_float(item.get("duration_sec")))
            elif status == "missed":
                missed_calls += 1
                sla = item.get("missed_call_sla") if isinstance(item.get("missed_call_sla"), dict) else {}
                sla_status = str(sla.get("status") or "").strip().lower()
                missed_team = str(sla.get("missed_team") or item.get("missed_call_owner_team") or "").strip()

                if missed_team:
                    missed_calls_by_team[missed_team] += 1
                if sla_status == "recovered_on_time":
                    missed_call_recovered += 1
                    if missed_team:
                        missed_recovered_by_team[missed_team] += 1
                elif sla_status in {"breached_late_recovery", "breached_no_recovery"}:
                    missed_call_sla_breached += 1
                    if missed_team:
                        missed_sla_breached_by_team[missed_team] += 1
                elif sla_status == "pending_before_deadline":
                    missed_call_sla_pending += 1
                    
            elif status == "customer_not_answered":
                customer_not_answered_calls += 1
            if item.get("agent"):
                agents[str(item.get("agent"))] += 1
        return compact_inner_dict(
            {
                "by_channel": dict(channel_counts),
                "by_flow": dict(flow_counts),
                "calls": {"connected": connected_calls, "missed": missed_calls, "missed_recovered_on_time": missed_call_recovered, "missed_sla_breached": missed_call_sla_breached, "missed_sla_pending": missed_call_sla_pending, "missed_by_team": dict(missed_calls_by_team), "missed_recovered_by_team": dict(missed_recovered_by_team), "missed_sla_breached_by_team": dict(missed_sla_breached_by_team), "talk_seconds": call_seconds, "customer_not_answered": customer_not_answered_calls,},
                "top_agents": [name for name, _ in agents.most_common(5)],
            }
        ) or {}

    def _compact_message_for_llm(self, item: Dict[str, Any], max_text_chars: int) -> Dict[str, Any]:
        if item.get("channel") == "call":
            return compact_inner_dict(
                {
                    "t": self._date_label(item.get("time"), with_time=True),
                    "ch": "call",
                    "flow": item.get("role_flow"),
                    "status": item.get("status"),
                    "dir": item.get("direction"),
                    "sec": int(_safe_float(item.get("duration_sec"))) if item.get("duration_sec") not in (None, "") else None,
                    "agent": item.get("agent"),
                    "role": item.get("agent_role"),
                    "missed_call_sla": item.get("missed_call_sla"),
                    "missed_call_recovered": item.get("missed_call_recovered"),
                    "missed_call_sla_breached": item.get("missed_call_sla_breached"),
                    "missed_call_owner_team": item.get("missed_call_owner_team"),
                    "missed_call_recovery_team": item.get("missed_call_recovery_team"),
                    "text": summarize_text(item.get("text") or item.get("transcript"), max_text_chars),
                }
            ) or {}
        compacted = {
            "t": self._date_label(item.get("time"), with_time=True),
            "ch": item.get("channel"),
            "flow": item.get("role_flow"),
            "subject": summarize_text(item.get("subject"), 90),
            "text": summarize_text(item.get("text"), max_text_chars),
        }
        if item.get("channel") == "whatsapp":
            compacted["role"] = item.get("business_role")
            compacted["agent"] = item.get("agent")
        return compact_inner_dict(compacted) or {}

    def _compact_conversation_for_llm(self, conversation: Dict[str, Any], *, max_messages: int = 10, max_text_chars: int = 140) -> Dict[str, Any]:
        messages = self._dedupe_messages(conversation.get("recent_messages") or [])
        selected = sorted(messages, key=lambda item: str(item.get("time") or ""), reverse=True)[: max(0, int(max_messages))]
        return compact_inner_dict(
            {
                "window": conversation.get("window"),
                "count": conversation.get("recent_message_count", len(messages)),
                "deduped_count": len(messages),
                "last_message_at": (conversation.get("freshness") or {}).get("last_message_at"),
                "stats": self._conversation_stats_for_llm(messages),
                "timeline": [msg for msg in (self._compact_message_for_llm(item, max_text_chars) for item in selected) if msg],
            }
        ) or {}

    def _compact_freshness_for_llm(self, freshness: Dict[str, Any]) -> Dict[str, Any]:
        return compact_inner_dict(
            {
                "generated_at": freshness.get("generated_at"),
                "booking": freshness.get("booking_last_updated_at"),
                "support": freshness.get("support_last_updated_at"),
                "conversation": freshness.get("conversation_last_updated_at"),
            }
        ) or {}

    @staticmethod
    def _compact_customer_for_llm(customer: Dict[str, Any]) -> Dict[str, Any]:
        ids = dict(customer.get("ids") or {})
        contacts = dict(customer.get("contacts") or {})
        return compact_inner_dict(
            {
                "tenant_id": customer.get("tenant_id"),
                "ids": ids,
                "contacts": contacts,
            }
        ) or {}

    def compact_for_llm(self, payload: Dict[str, Any], *, max_messages: int = 6, max_text_chars: int = 120) -> Dict[str, Any]:
        customer = payload.get("customer") or {}
        return compact_inner_dict(
            {
                "context_version": LLM_BRIEF_VERSION,
                "customer": self._compact_customer_for_llm(customer),
                "booking": self._compact_booking_for_llm(payload.get("booking") or {}),
                "support": self._compact_support_for_llm(payload.get("support") or {}),
                "conversation": self._compact_conversation_for_llm(payload.get("conversation") or {}, max_messages=max_messages, max_text_chars=max_text_chars),
            }
        ) or {}

    def build_llm_context(self, *, booking_id: int, conversation_days: int = 30, max_messages: int = 6, max_text_chars: int = 120) -> Dict[str, Any]:
        payload = self.build(booking_id=booking_id, conversation_days=conversation_days)
        return self.compact_for_llm(payload, max_messages=max_messages, max_text_chars=max_text_chars)

    # ------------------------------------------------------------------
    # Facts and public build
    # ------------------------------------------------------------------
    def _build_facts(
        self,
        *,
        scope: Dict[str, Any],
        booking_rows: List[Dict[str, Any]],
        linked_lead_rows: List[Dict[str, Any]],
        ticket_rows: List[Dict[str, Any]],
        conversation: Dict[str, Any],
    ) -> Dict[str, Any]:
        return compact_inner_dict(
            {
                "scope_basis": "booking_id_only",
                "counts": {
                    "booking_rows": len(booking_rows),
                    "linked_lead_rows": len(linked_lead_rows),
                    "ticket_rows": len(ticket_rows),
                    "conversation_messages": conversation.get("recent_message_count", 0),
                },
            }
        ) or {}

    def build(self, *, booking_id: int, conversation_days: int = 30, unrestricted: bool = False) -> Dict[str, Any]:
        if booking_id in (None, ""):
            raise ValueError("booking_id is required")
        booking_id_int = int(booking_id)

        booking_rows = self._booking_rows_for_id(booking_id_int)
        scope, linked_lead_rows = self._build_booking_scope(booking_id_int, booking_rows)
        universe = self._public_scope(scope)

        active_booking_row = self.timeline._pick_active_booking(booking_rows, _today_local(), universe=universe)
        booking = self.timeline._format_booking(active_booking_row)
        booking = self._fix_booking_state_using_early_cout(booking, active_booking_row)
        ticket_rows = self._ticket_rows_for_booking(universe)
        support = self._build_support(ticket_rows)
        conversation = self._normalize_conversation(self._conversation_for_booking(scope, conversation_days, unrestricted=unrestricted))
        facts = self._build_facts(
            scope=scope,
            booking_rows=booking_rows,
            linked_lead_rows=linked_lead_rows,
            ticket_rows=ticket_rows,
            conversation=conversation,
        )

        ids_payload = compact_inner_dict(
            {
                "booking": _coerce_id_list(universe.get("booking_ids") or [booking_id_int]),
                "user": _coerce_id_list(universe.get("user_ids") or []),
                "lead_from_booking": _coerce_id_list(universe.get("lead_ids") or []),
                "property": _coerce_id_list(universe.get("property_ids") or []),
                "person": _coerce_id_list(universe.get("person_ids") or []),
            }
        ) or {"booking": [booking_id_int]}
        contacts_payload = compact_inner_dict(
            {
                "phones": universe.get("phones"),
                "emails": universe.get("emails"),
            }
        ) or {}

        top_quality_flags: List[str] = []
        if not booking_rows:
            top_quality_flags.append("booking_not_found_in_staging_booking_confirm")
        if not universe.get("phones") and not universe.get("emails"):
            top_quality_flags.append("no_direct_booking_contacts_found")
        for section in (booking or {}, support or {}, conversation or {}):
            if isinstance(section, dict):
                top_quality_flags.extend(section.get("quality_flags") or [])

        important_quality_flags = sorted(
            {
                flag
                for flag in top_quality_flags
                if flag and flag != "contains_truncated_message_text"
            }
        )
        payload = {
            "context_version": BOOKING_BRIEF_VERSION,
            "customer": {
                "tenant_id": "rentmystay",
                "ids": ids_payload,
                "contacts": contacts_payload,
                "contact_sources": universe.get("contact_sources") or [],
            },
            "booking": booking,
            "support": support,
            "conversation": conversation,
            "facts": facts,
            "quality_flags": important_quality_flags,
        }
        return compact_inner_dict(payload) or payload


__all__ = ["CustomerBriefService"]


