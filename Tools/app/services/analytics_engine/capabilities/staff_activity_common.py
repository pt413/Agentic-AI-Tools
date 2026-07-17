from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

DEFAULT_SCHEMA = "AnalyticsEngine"
IST_OFFSET = timedelta(hours=5, minutes=30)
NON_DIGIT_RE = re.compile(r"\D+")
SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ACTIVE_TICKET_STATUSES = {"open", "in progress", "in_progress", "reopened", "pending", "assigned", "new"}
CLOSED_TICKET_STATUSES = {"closed", "resolved", "complete", "completed", "done"}
COMPANY_TO_CUSTOMER = {"outgoing", "outbound", "sent", "reply", "from_admin", "dialed", "dial", "out"}
CUSTOMER_TO_COMPANY = {"incoming", "inbound", "received", "from_customer", "missed", "receive", "in"}

ROLE_CONFIG: dict[str, dict[str, Any]] = {
    "generic": {
        "display": "Generic Staff",
        "building_ref_columns": [],
        "building_phone_columns": [],
        "metric_packs": ["common_activity"],
        "focus": [
            "Common own-name activity: calls, WhatsApp, and tickets where this staff member is assigned/resolved/closed actor.",
            "Do not infer role-specific performance if no matching role scope is available.",
        ],
    },
    "finance": {
        "display": "Finance",
        "building_ref_columns": ["finance_supervisor"],
        "building_phone_columns": [],
        "metric_packs": ["common_activity", "finance_actions", "assigned_buildings", "tickets"],
        "focus": [
            "Finance follow-ups and invoice/payment rows touched by this staff member.",
            "Customer/payment communication quality and closure clarity.",
            "Tickets handled directly by this staff member, especially finance/payment related tickets.",
        ],
    },
    "caretaker": {
        "display": "Caretaker",
        "building_ref_columns": ["caretaker"],
        "building_phone_columns": [],
        "metric_packs": ["common_activity", "assigned_buildings", "assigned_properties", "field_feedback", "site_visits", "property_marks", "tickets"],
        "focus": [
            "Assigned building/property coverage and current availability awareness.",
            "Track non-currently-available units separately as availability-date/current-occupancy inventory; count against quality only when avl_date has passed, checkout is completed, and the unit is still not bookable.",
            "Check-in/checkout feedback, cleaning/stay/building ratings, and property verification marks.",
            "Own tickets plus field communication and follow-up quality.",
        ],
    },
    "sales": {
        "display": "Sales",
        "building_ref_columns": ["sales"],
        "building_phone_columns": ["sales_phone", "sales_normalized_phone"],
        "lead_ref_columns": ["executive_id", "assigned_to", "added_by", "generated_by"],
        "booking_ref_columns": ["created_by"],
        "metric_packs": ["common_activity", "sales_pipeline", "site_visits", "bookings", "travel_cart", "assigned_buildings"],
        "focus": [
            "Lead ownership, follow-up volume, site-visit activity, and booking conversion evidence.",
            "Missed/zero-duration calls and WhatsApp response gaps for prospects/customers.",
            "Handoff quality from lead to booking or onboarding.",
        ],
    },
    "onboarding": {
        "display": "Onboarding",
        "building_ref_columns": [],
        "building_phone_columns": [],
        "lead_ref_columns": ["executive_id", "assigned_to", "added_by"],
        "booking_ref_columns": ["created_by"],
        "metric_packs": ["common_activity", "onboarding", "bookings", "field_feedback", "site_visits"],
        "focus": [
            "Booking onboarding readiness, check-in feedback, and early customer activation risks.",
            "Own communication around check-in/support handoff.",
            "Tickets or follow-ups directly owned by this staff member.",
        ],
    },
    "technical": {
        "display": "Technical",
        "building_ref_columns": [],
        "building_phone_columns": [],
        "metric_packs": ["common_activity", "technical_tickets", "property_marks"],
        "focus": [
            "Technical/support tickets handled directly by this staff member.",
            "Repeated issue categories, reopen flags, active days, closure comments and ratings.",
            "Property/asset verification or update marks made by this staff member.",
        ],
    },
    "ops_team": {
        "display": "Ops Team",
        "building_ref_columns": ["supervisor", "ops_manager"],
        "building_phone_columns": [],
        "metric_packs": ["common_activity", "assigned_buildings", "assigned_properties", "field_feedback", "site_visits", "property_marks", "tickets"],
        "focus": [
            "Operational coverage across assigned buildings/properties.",
            "Track non-currently-available units separately as availability-date/current-occupancy inventory; count against quality only when avl_date has passed, checkout is completed, and the unit is still not bookable.",
            "Tickets, check-in/checkout feedback, site visits and property marks linked to this staff member.",
            "Communication gaps and delayed ownership or handoff issues.",
        ],
    },
    "marketing": {
        "display": "Marketing",
        "building_ref_columns": ["marketing"],
        "building_phone_columns": [],
        "lead_ref_columns": ["generated_by", "added_by"],
        "metric_packs": ["common_activity", "marketing_leads", "travel_cart", "bookings", "assigned_buildings"],
        "focus": [
            "Lead generation and source/handoff quality.",
            "Whether generated leads moved into site visits, booking attempts, or bookings.",
            "Own communication and direct ticket activity only; avoid judging field-operation metrics unless evidence exists.",
        ],
    },
}

ROLE_ALIASES: dict[str, str] = {
    "auto": "auto",
    "all": "generic",
    "generic": "generic",
    "staff": "generic",
    "admin": "generic",
    "finance": "finance",
    "finance team": "finance",
    "caretaker": "caretaker",
    "care taker": "caretaker",
    "sales": "sales",
    "sales team": "sales",
    "onboarding": "onboarding",
    "on boarding": "onboarding",
    "technical": "technical",
    "tech": "technical",
    "ops": "ops_team",
    "ops team": "ops_team",
    "operations": "ops_team",
    "operations team": "ops_team",
    "marketing": "marketing",
    "marketing team": "marketing",
}


def normalize_role_scope(role: Any = None, staff_team: Any = None) -> str:
    """Return canonical role scope. `auto` uses staff.team when possible."""
    raw = lower_ref(role) or "auto"
    if raw == "auto":
        raw = lower_ref(staff_team) or "generic"
    return ROLE_ALIASES.get(raw, raw if raw in ROLE_CONFIG else "generic")


def role_display_name(role_scope: str, staff_team: Any = None) -> str:
    config = ROLE_CONFIG.get(role_scope) or ROLE_CONFIG["generic"]
    return str(config.get("display") or staff_team or role_scope or "Generic Staff")


def _safe_ident(value: str) -> str:
    text_value = str(value or "").strip()
    if not SAFE_IDENT_RE.fullmatch(text_value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return text_value


def schema_ident(schema: str) -> str:
    return f'"{_safe_ident(schema)}"'


def table_ref(schema: str, table_name: str) -> str:
    return f"{schema_ident(schema)}.{_safe_ident(table_name)}"


def now_ist_naive() -> datetime:
    return datetime.utcnow() + IST_OFFSET


def _date_window(days: int) -> tuple[datetime, datetime, str]:
    safe_days = max(1, int(days or 30))
    end_dt = now_ist_naive()
    start_dt = end_dt - timedelta(days=safe_days)
    return start_dt, end_dt, f"last {safe_days} day(s)"


def clean_text(value: Any, max_len: int = 280) -> str:
    if value in (None, ""):
        return ""
    text_value = re.sub(r"\s+", " ", str(value)).strip()
    if not text_value:
        return ""
    if max_len and len(text_value) > max_len:
        return text_value[: max_len - 3].rstrip() + "..."
    return text_value


def norm_digits(value: Any) -> str:
    return NON_DIGIT_RE.sub("", str(value or ""))


def phone_last10(value: Any) -> Optional[str]:
    digits = norm_digits(value)
    return digits[-10:] if len(digits) >= 10 else None


def show_phone(value: Any) -> Optional[str]:
    last10 = phone_last10(value)
    return f"91{last10}" if last10 else None


def norm_email(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    email = str(value).strip().lower()
    return email if "@" in email else None


def lower_ref(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text_value = " ".join(str(value).strip().lower().split())
    return text_value or None


def coerce_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text_value = str(value).strip()
    if not text_value:
        return None
    try:
        return datetime.fromisoformat(text_value.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def coerce_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = coerce_datetime(value)
    if parsed:
        return parsed.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def fmt_dt(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def fmt_duration(value: Any) -> str:
    try:
        seconds = int(float(value or 0))
    except Exception:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {rem}s" if rem else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def build_in_params(values: Sequence[Any], prefix: str) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {}
    holders: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in (None, ""):
            continue
        key_value = str(value)
        if key_value in seen:
            continue
        seen.add(key_value)
        key = f"{prefix}_{len(holders)}"
        holders.append(f":{key}")
        params[key] = value
    return ("(" + ", ".join(holders) + ")", params) if holders else ("(NULL)", {})


def compact_dict(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in (value or {}).items():
        if item in (None, "", [], {}, ()):  # keep 0/False
            continue
        if isinstance(item, dict):
            nested = compact_dict(item)
            if nested:
                out[key] = nested
        elif isinstance(item, list):
            cleaned_list = []
            for child in item:
                if isinstance(child, dict):
                    nested = compact_dict(child)
                    if nested:
                        cleaned_list.append(nested)
                elif child not in (None, "", [], {}, ()):  # keep 0/False
                    cleaned_list.append(child)
            if cleaned_list:
                out[key] = cleaned_list
        else:
            out[key] = item
    return out


def avg_numeric(rows: Sequence[dict[str, Any]], key: str) -> Optional[float]:
    values: list[float] = []
    for row in rows:
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            values.append(float(value))
        except Exception:
            continue
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def direction_flow(value: Any, *, staff_role: str = "staff") -> str:
    normalized = str(value or "").strip().lower()
    if normalized in COMPANY_TO_CUSTOMER:
        return f"{staff_role}_to_counterparty"
    if normalized in CUSTOMER_TO_COMPANY:
        return f"counterparty_to_{staff_role}"
    return "unknown"


def boolish(value: Any) -> Optional[bool]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    text_value = str(value).strip().lower()
    if text_value in {"1", "true", "t", "yes", "y", "active"}:
        return True
    if text_value in {"0", "false", "f", "no", "n", "inactive"}:
        return False
    return None


def property_unit_display(row: dict[str, Any]) -> str:
    """Preferred user-facing unit name.

    staging_property_unit.unit_name is the standard readable identity, e.g.
    1B-Singasandra-Nandan Homes-G1. IDs stay in raw rows for joins/debug,
    but compact/evidence views should show this label wherever possible.
    """
    for key in (
        "unit_name",
        "property_unit_name",
        "prop_name",
        "display_property_name",
        "listing_title",
    ):
        value = clean_text((row or {}).get(key), 160)
        if value:
            return value
    # Do not fall back to unit_number/prop_id in compact evidence; those are
    # not the business-readable standard names.
    return "unit_name_unavailable"


def account_type_display(value: Any) -> str:
    """System account type from is_admin flag."""
    if boolish(value) is True:
        return "Admin"
    return "User"


def staff_admin_role_display(staff: dict[str, Any]) -> str:
    """Business role/team, e.g. Caretaker, Sales, Finance."""
    team = clean_text((staff or {}).get("team"), 80)
    if team:
        return team
    return account_type_display((staff or {}).get("is_admin"))


def staff_is_admin_display(staff: dict[str, Any]) -> str:
    """Role-aware is_admin display: User (Caretaker), Admin (Finance), etc."""
    account_type = account_type_display((staff or {}).get("is_admin"))
    role = staff_admin_role_display(staff)
    if role and role != account_type:
        return f"{account_type} ({role})"
    return account_type


@dataclass
class StaffActivityBaseService:
    db: Session
    schema: str = DEFAULT_SCHEMA
    _table_exists_cache: dict[str, bool] = field(default_factory=dict, init=False)
    _columns_cache: dict[str, set[str]] = field(default_factory=dict, init=False)
    _staff_phone_directory_cache: Optional[dict[str, dict[str, Any]]] = field(default=None, init=False)
    _line_assignment_directory_cache: Optional[dict[str, dict[str, Any]]] = field(default=None, init=False)

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------
    def table_exists(self, table_name: str) -> bool:
        safe_table = _safe_ident(table_name)
        cache_key = f"{self.schema}.{safe_table}"
        if cache_key not in self._table_exists_cache:
            row = self.db.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema = :schema_name
                          AND table_name = :table_name
                    ) AS present
                    """
                ),
                {"schema_name": self.schema, "table_name": safe_table},
            ).mappings().fetchone()
            self._table_exists_cache[cache_key] = bool(row and row.get("present"))
        return self._table_exists_cache[cache_key]

    def table_columns(self, table_name: str) -> set[str]:
        safe_table = _safe_ident(table_name)
        cache_key = f"{self.schema}.{safe_table}:columns"
        if cache_key in self._columns_cache:
            return self._columns_cache[cache_key]
        if not self.table_exists(safe_table):
            self._columns_cache[cache_key] = set()
            return set()
        rows = self.db.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = :schema_name
                  AND table_name = :table_name
                """
            ),
            {"schema_name": self.schema, "table_name": safe_table},
        ).mappings().fetchall()
        columns = {str(row["column_name"]) for row in rows if row.get("column_name")}
        self._columns_cache[cache_key] = columns
        return columns

    def rows(self, sql: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(text(sql), params or {}).mappings().fetchall()]

    def one(self, sql: str, params: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        row = self.db.execute(text(sql), params or {}).mappings().fetchone()
        return dict(row) if row else None

    def select_exprs(self, table_name: str, alias: str, columns: Sequence[str], *, prefix: str = "") -> str:
        available = self.table_columns(table_name)
        parts: list[str] = []
        for column in columns:
            safe_column = _safe_ident(column)
            out_name = f"{prefix}{safe_column}" if prefix else safe_column
            if safe_column in available:
                parts.append(f"{alias}.{safe_column} AS {out_name}")
            else:
                parts.append(f"NULL AS {out_name}")
        return ",\n               ".join(parts)

    def coalesce_existing(self, table_name: str, alias: str, columns: Sequence[str]) -> str:
        available = self.table_columns(table_name)
        existing = [f"{alias}.{_safe_ident(column)}" for column in columns if _safe_ident(column) in available]
        if not existing:
            return "NULL"
        if len(existing) == 1:
            return existing[0]
        return f"COALESCE({', '.join(existing)})"

    # ------------------------------------------------------------------
    # Staff resolution / profile
    # ------------------------------------------------------------------
    def _staff_from_user_row(
        self,
        row: dict[str, Any],
        *,
        resolved_from: str = "staging_user_account",
        line_assignment: Optional[dict[str, Any]] = None,
        input_phone: Optional[str] = None,
    ) -> dict[str, Any]:
        return compact_dict(
            {
                "staff_id": row.get("source_id"),
                "source_id": row.get("source_id"),
                "username": row.get("username"),
                "email": row.get("email"),
                "phone_number": row.get("phone_number"),
                "normalized_phone": show_phone(row.get("normalized_phone") or row.get("phone_number")),
                "is_admin": row.get("is_admin"),
                "team": row.get("team") or (line_assignment or {}).get("team"),
                "active": row.get("active") or (line_assignment or {}).get("login_status"),
                "created_on": row.get("created_on"),
                "last_login_time": row.get("last_login_time"),
                "synced_at": row.get("synced_at"),
                "resolved_from": resolved_from,
                "input_phone": show_phone(input_phone),
                "input_phone_is_pooled_line": bool(line_assignment),
                "line_assignment": line_assignment,
            }
        )

    def _staff_from_line_assignment(self, assignment: dict[str, Any], *, input_phone: Optional[str] = None) -> dict[str, Any]:
        """Fallback staff profile when MobileTagging has a current line owner but user_account is missing."""
        return compact_dict(
            {
                "staff_id": None,
                "source_id": None,
                "username": assignment.get("username") or assignment.get("tag_to"),
                "email": None,
                "phone_number": None,
                "normalized_phone": None,
                "is_admin": None,
                "team": assignment.get("team"),
                "active": assignment.get("login_status"),
                "created_on": None,
                "last_login_time": None,
                "synced_at": assignment.get("synced_at"),
                "resolved_from": "staff_phone_assignment_only",
                "input_phone": show_phone(input_phone or assignment.get("normalized_phone") or assignment.get("phone")),
                "input_phone_is_pooled_line": True,
                "line_assignment": assignment,
            }
        )

    def _user_account_row_by_username(self, username_value: Any) -> Optional[dict[str, Any]]:
        username_ref = lower_ref(username_value)
        if not username_ref or not self.table_exists("staging_user_account"):
            return None
        columns = [
            "source_id", "username", "email", "phone_number", "normalized_phone",
            "is_admin", "team", "active", "created_on", "last_login_time", "synced_at",
        ]
        select_list = self.select_exprs("staging_user_account", "ua", columns)
        return self.one(
            f"""
            SELECT {select_list}
            FROM {table_ref(self.schema, "staging_user_account")} ua
            WHERE LOWER(TRIM(COALESCE(ua.username::text, ''))) = :username
            ORDER BY
                CASE WHEN LOWER(COALESCE(ua.active::text, '')) = 'active' THEN 0 ELSE 1 END,
                ua.source_id ASC
            LIMIT 1
            """,
            {"username": username_ref},
        )

    def _line_assignment_directory(self) -> dict[str, dict[str, Any]]:
        """Return current pooled/office-line assignment by phone10 from staging_staff_phone_assignment."""
        if self._line_assignment_directory_cache is not None:
            return self._line_assignment_directory_cache

        table_name = "staging_staff_phone_assignment"
        directory: dict[str, dict[str, Any]] = {}
        if not self.table_exists(table_name):
            self._line_assignment_directory_cache = directory
            return directory

        columns = self.table_columns(table_name)
        select_columns = [
            "phone", "normalized_phone", "phone10", "tag_to", "city", "team", "login_status",
            "username", "auto_lead", "assign_lead", "wa_priority", "last_update_time", "synced_at",
        ]
        select_list = self.select_exprs(table_name, "a", select_columns)
        phone10_expr = (
            "RIGHT(REGEXP_REPLACE(COALESCE(a.phone10::text, a.normalized_phone::text, a.phone::text, ''), '\\D', '', 'g'), 10)"
            if "phone10" in columns
            else "RIGHT(REGEXP_REPLACE(COALESCE(a.normalized_phone::text, a.phone::text, ''), '\\D', '', 'g'), 10)"
        )
        order_exprs = []
        if "last_update_time" in columns:
            order_exprs.append("a.last_update_time DESC NULLS LAST")
        if "synced_at" in columns:
            order_exprs.append("a.synced_at DESC NULLS LAST")
        order_sql = ", ".join(order_exprs) or "1"
        rows = self.rows(
            f"""
            SELECT {select_list}, {phone10_expr} AS resolved_phone10
            FROM {table_ref(self.schema, table_name)} a
            WHERE LENGTH({phone10_expr}) = 10
            ORDER BY {order_sql}
            """
        )
        for row in rows:
            p10 = phone_last10(row.get("phone10") or row.get("resolved_phone10") or row.get("normalized_phone") or row.get("phone"))
            if not p10 or p10 in directory:
                continue
            directory[p10] = compact_dict(
                {
                    "phone": show_phone(row.get("phone") or row.get("normalized_phone")),
                    "normalized_phone": show_phone(row.get("normalized_phone") or row.get("phone")),
                    "phone10": p10,
                    "tag_to": row.get("tag_to"),
                    "city": row.get("city"),
                    "team": row.get("team"),
                    "login_status": row.get("login_status"),
                    "username": row.get("username"),
                    "auto_lead": row.get("auto_lead"),
                    "assign_lead": row.get("assign_lead"),
                    "wa_priority": row.get("wa_priority"),
                    "last_update_time": row.get("last_update_time"),
                    "synced_at": row.get("synced_at"),
                }
            )

        self._line_assignment_directory_cache = directory
        return directory

    def _phone_assignment_by_phone10(self, phone10_value: Any) -> Optional[dict[str, Any]]:
        p10 = phone_last10(phone10_value)
        if not p10:
            return None
        return self._line_assignment_directory().get(p10)

    def _line_assignment_by_username(self, username_value: Any) -> Optional[dict[str, Any]]:
        username_ref = lower_ref(username_value)
        if not username_ref:
            return None
        for assignment in self._line_assignment_directory().values():
            if lower_ref(assignment.get("username")) == username_ref or lower_ref(assignment.get("tag_to")) == username_ref:
                return assignment
        return None

    def _staff_line_assignments(self, staff: dict[str, Any]) -> list[dict[str, Any]]:
        refs = set(self.staff_refs(staff))
        direct_assignment = staff.get("line_assignment") if isinstance(staff.get("line_assignment"), dict) else None
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        if direct_assignment:
            p10 = phone_last10(direct_assignment.get("phone10") or direct_assignment.get("normalized_phone") or direct_assignment.get("phone"))
            if p10:
                seen.add(p10)
                out.append(direct_assignment)
        if refs:
            for assignment in self._line_assignment_directory().values():
                if lower_ref(assignment.get("username")) in refs or lower_ref(assignment.get("tag_to")) in refs:
                    p10 = phone_last10(assignment.get("phone10") or assignment.get("normalized_phone") or assignment.get("phone"))
                    if p10 and p10 not in seen:
                        seen.add(p10)
                        out.append(assignment)
        return out

    def staff_line_phone10s(self, staff: dict[str, Any]) -> list[str]:
        values: list[str] = []
        for assignment in self._staff_line_assignments(staff):
            p10 = phone_last10(assignment.get("phone10") or assignment.get("normalized_phone") or assignment.get("phone"))
            if p10 and p10 not in values:
                values.append(p10)
        return values

    def resolve_staff(
        self,
        *,
        staff_id: Optional[int] = None,
        username: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
    ) -> dict[str, Any]:
        table_name = "staging_user_account"
        if not self.table_exists(table_name):
            raise ValueError(f"{self.schema}.{table_name} does not exist. Run admin_user_account sync first.")

        phone10 = phone_last10(phone)
        has_person_identifier = any(value not in (None, "") for value in (staff_id, username, email))

        # Pooled/office line lookup: if the input is only a phone number, prefer
        # MobileTagging's current assignment over permanent user_account phone
        # fields. A pooled line is not a stable staff identity.
        if phone10 and not has_person_identifier:
            assignment = self._phone_assignment_by_phone10(phone10)
            if assignment:
                assignment_username = assignment.get("username") or assignment.get("tag_to")
                user_row = self._user_account_row_by_username(assignment_username)
                if user_row:
                    return self._staff_from_user_row(
                        user_row,
                        resolved_from="staff_phone_assignment",
                        line_assignment=assignment,
                        input_phone=phone,
                    )
                if assignment_username:
                    return self._staff_from_line_assignment(assignment, input_phone=phone)

        columns = [
            "source_id", "username", "email", "phone_number", "normalized_phone",
            "is_admin", "team", "active", "created_on", "last_login_time", "synced_at",
        ]
        select_list = self.select_exprs(table_name, "ua", columns)
        conds: list[str] = []
        params: dict[str, Any] = {}

        if staff_id not in (None, ""):
            conds.append("ua.source_id = :staff_id")
            params["staff_id"] = int(staff_id)
        if username not in (None, ""):
            conds.append("LOWER(TRIM(COALESCE(ua.username::text, ''))) = :username")
            params["username"] = lower_ref(username)
        if email not in (None, ""):
            conds.append("LOWER(TRIM(COALESCE(ua.email::text, ''))) = :email")
            params["email"] = lower_ref(email)
        if phone10:
            conds.append(
                "(RIGHT(REGEXP_REPLACE(COALESCE(ua.normalized_phone::text, ''), '\\D', '', 'g'), 10) = :phone10 "
                " OR RIGHT(REGEXP_REPLACE(COALESCE(ua.phone_number::text, ''), '\\D', '', 'g'), 10) = :phone10)"
            )
            params["phone10"] = phone10

        if not conds:
            raise ValueError("Provide one staff identifier: staff_id, username, email, or phone.")

        where_sql = " OR ".join(f"({cond})" for cond in conds)
        def _lookup_user_row(extra_filter: str = "") -> Optional[dict[str, Any]]:
            return self.one(
                f"""
                SELECT {select_list}
                FROM {table_ref(self.schema, table_name)} ua
                WHERE {where_sql}
                  {extra_filter}
                ORDER BY
                    CASE WHEN LOWER(COALESCE(ua.active::text, '')) = 'active' THEN 0 ELSE 1 END,
                    ua.source_id ASC
                LIMIT 1
                """,
                params,
            )

        staff_scope_filter = ""
        if phone10 and not has_person_identifier and "team" in self.table_columns(table_name):
            staff_scope_filter = "AND COALESCE(NULLIF(TRIM(ua.team::text), ''), '') <> ''"

        row = _lookup_user_row(staff_scope_filter)
        if row is None and staff_scope_filter:
            row = _lookup_user_row()
        if row:
            return self._staff_from_user_row(row, resolved_from="staging_user_account", input_phone=phone)

        # Username fallback: allow a current MobileTagging owner to resolve even
        # when staging_user_account has not caught up yet.
        assignment = self._line_assignment_by_username(username) if username not in (None, "") else None
        if assignment:
            user_row = self._user_account_row_by_username(assignment.get("username") or assignment.get("tag_to"))
            if user_row:
                return self._staff_from_user_row(user_row, resolved_from="staff_phone_assignment", line_assignment=assignment, input_phone=phone)
            return self._staff_from_line_assignment(assignment, input_phone=phone)

        raise ValueError("No staff/admin user found for the provided identifier.")

    def list_staff(self, *, team: Optional[str] = None, active: bool = True, limit: int = 500) -> list[dict[str, Any]]:
        table_name = "staging_user_account"
        if not self.table_exists(table_name):
            return []
        columns = ["source_id", "username", "email", "phone_number", "normalized_phone", "is_admin", "team", "active", "last_login_time"]
        select_list = self.select_exprs(table_name, "ua", columns)
        conds: list[str] = []
        params: dict[str, Any] = {"limit_n": int(limit)}
        real_columns = self.table_columns(table_name)

        # Staff dashboard should exclude generic consumer users. Keep rows that
        # have a business team, or explicit admin accounts when team is blank.
        staff_scope_conds: list[str] = []
        if "team" in real_columns:
            staff_scope_conds.append("COALESCE(NULLIF(TRIM(ua.team::text), ''), '') <> ''")
        if "is_admin" in real_columns:
            staff_scope_conds.append("LOWER(COALESCE(ua.is_admin::text, '')) IN ('1', 'true', 't', 'yes', 'y', 'admin')")
        if staff_scope_conds:
            conds.append("(" + " OR ".join(staff_scope_conds) + ")")

        if team not in (None, "", "all"):
            conds.append("LOWER(COALESCE(ua.team::text, '')) = :team")
            params["team"] = lower_ref(team)
        if active:
            conds.append("LOWER(COALESCE(ua.active::text, '')) = 'active'")
        where_sql = f"WHERE {' AND '.join(conds)}" if conds else ""
        rows = self.rows(
            f"""
            SELECT {select_list}
            FROM {table_ref(self.schema, table_name)} ua
            {where_sql}
            ORDER BY LOWER(COALESCE(ua.team::text, '')), LOWER(COALESCE(ua.username::text, ''))
            LIMIT :limit_n
            """,
            params,
        )
        out = []
        for row in rows:
            out.append(
                compact_dict(
                    {
                        "staff_id": row.get("source_id"),
                        "username": row.get("username"),
                        "email": row.get("email"),
                        "phone": show_phone(row.get("normalized_phone") or row.get("phone_number")),
                        "is_admin": row.get("is_admin"),
                        "team": row.get("team"),
                        "active": row.get("active"),
                        "last_login_time": row.get("last_login_time"),
                    }
                )
            )
        return out

    def staff_refs(self, staff: dict[str, Any]) -> list[str]:
        refs: list[str] = []
        for value in (staff.get("username"), staff.get("email"), staff.get("source_id"), staff.get("staff_id")):
            ref = lower_ref(value)
            if ref and ref not in refs:
                refs.append(ref)
        return refs

    def staff_phone10s(self, staff: dict[str, Any]) -> list[str]:
        values = []
        for key in ("normalized_phone", "phone_number"):
            p10 = phone_last10(staff.get(key))
            if p10 and p10 not in values:
                values.append(p10)
        return values

    # ------------------------------------------------------------------
    # Staff/internal phone helpers
    # ------------------------------------------------------------------
    def _staff_phone_directory(self) -> dict[str, dict[str, Any]]:
        """Return last-10 phone -> staff profile for internal/admin-number labelling."""
        if self._staff_phone_directory_cache is not None:
            return self._staff_phone_directory_cache

        table_name = "staging_user_account"
        directory: dict[str, dict[str, Any]] = {}
        if not self.table_exists(table_name):
            self._staff_phone_directory_cache = directory
            return directory

        columns = ["source_id", "username", "email", "phone_number", "normalized_phone", "team", "is_admin", "active"]
        select_list = self.select_exprs(table_name, "ua", columns)
        team_filter = (
            "AND COALESCE(NULLIF(TRIM(ua.team::text), ''), '') NOT IN ('', '0', 'null', 'NULL', 'None', 'none')"
            if "team" in self.table_columns(table_name)
            else ""
        )
        rows = self.rows(
            f"""
            SELECT {select_list}
            FROM {table_ref(self.schema, table_name)} ua
            WHERE (
                    LENGTH(REGEXP_REPLACE(COALESCE(ua.normalized_phone::text, ''), '\\D', '', 'g')) >= 10
                 OR LENGTH(REGEXP_REPLACE(COALESCE(ua.phone_number::text, ''), '\\D', '', 'g')) >= 10
                  )
              {team_filter}
            ORDER BY
                CASE WHEN LOWER(COALESCE(ua.active::text, '')) = 'active' THEN 0 ELSE 1 END,
                ua.source_id ASC
            """
        )
        for row in rows:
            entry = compact_dict(
                {
                    "staff_id": row.get("source_id"),
                    "username": row.get("username"),
                    "team": row.get("team"),
                    "is_admin": row.get("is_admin"),
                    "active": row.get("active"),
                }
            )
            for phone_value in (row.get("normalized_phone"), row.get("phone_number")):
                p10 = phone_last10(phone_value)
                if p10 and p10 not in directory:
                    directory[p10] = entry

        self._staff_phone_directory_cache = directory
        return directory

    def _phone_identity(self, value: Any) -> dict[str, Any]:
        number = show_phone(value)
        if not number:
            return {}
        p10 = phone_last10(number)
        assignment = self._phone_assignment_by_phone10(p10)
        if assignment:
            return compact_dict(
                {
                    "number": number,
                    "type": "internal",
                    "name": assignment.get("username") or assignment.get("tag_to") or "pooled_line",
                    "team": assignment.get("team"),
                    "line_type": "pooled",
                    "pooled_username": assignment.get("username"),
                    "pooled_team": assignment.get("team"),
                    "line_assignment": assignment,
                }
            )
        staff_row = self._staff_phone_directory().get(p10 or "")
        if staff_row:
            return compact_dict(
                {
                    "number": number,
                    "type": "internal",
                    "name": staff_row.get("username"),
                    "team": staff_row.get("team"),
                    "staff_id": staff_row.get("staff_id"),
                    "line_type": "personal_or_direct",
                }
            )
        return {"number": number, "type": "external"}

    def _line_identity(self, value: Any) -> dict[str, Any]:
        number = show_phone(value)
        if not number:
            return {}
        p10 = phone_last10(number)
        assignment = self._phone_assignment_by_phone10(p10)
        return compact_dict(
            {
                "number": number,
                "phone10": p10,
                "line_type": "pooled" if assignment else "unmapped_office_or_direct",
                "pooled_username": (assignment or {}).get("username"),
                "pooled_team": (assignment or {}).get("team"),
                "pooled_tag_to": (assignment or {}).get("tag_to"),
                "line_assignment": assignment,
            }
        )

    def _call_actor_party(self, row: dict[str, Any]) -> dict[str, Any]:
        """Actual call actor. Prefer call-log executive fields; use line assignment only as fallback."""
        line = self._line_identity(row.get("sales_phone"))
        executive_name = clean_text(row.get("executive_name"), 100)
        executive_id = clean_text(row.get("executive_id"), 100)
        if executive_name or executive_id:
            return compact_dict(
                {
                    "number": line.get("number"),
                    "type": "internal",
                    "name": executive_name or executive_id,
                    "staff_id": executive_id,
                    "team": None,
                    "actor_source": "call_log_executive",
                    "attribution": "call_log_executive",
                    "phone_line": line,
                    "line_assignment": line.get("line_assignment"),
                }
            )
        assignment = line.get("line_assignment") if isinstance(line.get("line_assignment"), dict) else None
        if assignment:
            return compact_dict(
                {
                    "number": line.get("number"),
                    "type": "internal",
                    "name": assignment.get("username") or assignment.get("tag_to") or "unknown",
                    "team": assignment.get("team"),
                    "actor_source": "staff_phone_assignment",
                    "attribution": "fallback_from_line_assignment",
                    "phone_line": line,
                    "line_assignment": assignment,
                }
            )
        if line.get("number"):
            return compact_dict(
                {
                    "number": line.get("number"),
                    "type": "internal",
                    "name": "unknown",
                    "actor_source": "unknown_line_actor",
                    "attribution": "unknown_actor",
                    "phone_line": line,
                }
            )
        return {}

    @staticmethod
    def _party_label(party: dict[str, Any]) -> str:
        if not party:
            return "unknown"
        name = party.get("name") or party.get("type") or "unknown"
        number = party.get("number") or ""
        return f"{name} ({number})" if number else str(name)

    def _call_parties(self, row: dict[str, Any]) -> dict[str, Any]:
        staff_party = self._call_actor_party(row)
        counterparty = self._phone_identity(row.get("counterparty_phone"))
        raw_flow = direction_flow(row.get("call_direction"), staff_role="staff")
        direction_known = raw_flow != "unknown"

        if raw_flow == "staff_to_counterparty":
            caller = dict(staff_party or {})
            receiver = dict(counterparty or {})
            caller_role = "staff_actor"
            receiver_role = "counterparty"
            flow = raw_flow
        elif raw_flow == "counterparty_to_staff":
            caller = dict(counterparty or {})
            receiver = dict(staff_party or {})
            caller_role = "counterparty"
            receiver_role = "staff_actor"
            flow = raw_flow
        else:
            # The recording source sometimes has no reliable incoming/outgoing marker.
            # Do not invent who dialed whom. Keep the staff and counterparty sides
            # visible and mark the relation as direction_unknown in the one-line view.
            caller = dict(staff_party or {})
            receiver = dict(counterparty or {})
            caller_role = "staff_actor"
            receiver_role = "counterparty"
            if caller.get("type") == "internal" and receiver.get("type") == "internal":
                flow = "staff_to_staff_direction_unknown"
            elif caller or receiver:
                flow = "staff_counterparty_direction_unknown"
            else:
                flow = "direction_unknown"

        if caller:
            caller["role"] = caller_role
        if receiver:
            receiver["role"] = receiver_role

        call_type = "internal" if caller.get("type") == "internal" and receiver.get("type") == "internal" else "external"
        phone_line = staff_party.get("phone_line") or self._line_identity(row.get("sales_phone"))
        return compact_dict(
            {
                "flow": flow,
                "raw_flow": raw_flow,
                "direction_known": direction_known,
                "call_type": call_type,
                "caller": caller,
                "receiver": receiver,
                "phone_line": phone_line,
                "line_assignment": phone_line.get("line_assignment") if isinstance(phone_line, dict) else None,
                "attribution": staff_party.get("attribution"),
            }
        )

    # ------------------------------------------------------------------
    # Caretaker master data
    # ------------------------------------------------------------------
    def collect_assigned_buildings(self, staff: dict[str, Any], role_scope: str) -> list[dict[str, Any]]:
        table_name = "staging_buildings"
        if not self.table_exists(table_name):
            return []

        config = ROLE_CONFIG.get(role_scope) or ROLE_CONFIG["generic"]
        columns = self.table_columns(table_name)
        ref_columns = [col for col in config.get("building_ref_columns", []) if col in columns]
        phone_columns = [col for col in config.get("building_phone_columns", []) if col in columns]
        if not ref_columns and not phone_columns:
            return []

        refs = self.staff_refs(staff)
        phone10s = list(dict.fromkeys(self.staff_phone10s(staff) + self.staff_line_phone10s(staff)))
        conds: list[str] = []
        params: dict[str, Any] = {}
        if refs and ref_columns:
            in_sql, in_params = build_in_params(refs, "bref")
            params.update(in_params)
            conds.extend([f"LOWER(TRIM(COALESCE(b.{_safe_ident(col)}::text, ''))) IN {in_sql}" for col in ref_columns])
        if phone10s and phone_columns:
            in_sql, in_params = build_in_params(phone10s, "bphone")
            params.update(in_params)
            conds.extend([
                f"RIGHT(REGEXP_REPLACE(COALESCE(b.{_safe_ident(col)}::text, ''), '\\D', '', 'g'), 10) IN {in_sql}"
                for col in phone_columns
            ])
        if not conds:
            return []

        building_columns = [
            "source_id", "building_id", "building_name", "city", "area", "address", "pincode",
            "glat", "glng", "direction_note", "caretaker", "supervisor", "ops_manager",
            "finance_supervisor", "sales", "sales_phone", "sales_normalized_phone", "marketing",
            "building_status", "show_tenants", "rent_model", "future_booking_plan", "agreement_date",
            "agreement_renewable_date", "position_date", "grace_period", "wifi_account_id", "wifi_details",
            "wifi_comment", "wifi_expire_on", "wifi_recharge", "updated_by", "updated_on", "synced_at",
        ]
        select_list = self.select_exprs(table_name, "b", building_columns)
        building_status_filter = "AND COALESCE(b.building_status, 0) <> 1" if "building_status" in columns else ""
        where_sql = " OR ".join(f"({cond})" for cond in conds)
        rows = self.rows(
            f"""
            SELECT {select_list}
            FROM {table_ref(self.schema, table_name)} b
            WHERE ({where_sql})
              {building_status_filter}
            ORDER BY LOWER(COALESCE(b.building_name::text, '')), b.building_id NULLS LAST, b.source_id NULLS LAST
            """,
            params,
        )

        out: list[dict[str, Any]] = []
        ref_set = set(refs)
        phone_set = set(phone10s)
        for row in rows:
            match_columns: list[str] = []
            for col in ref_columns:
                if lower_ref(row.get(col)) in ref_set:
                    match_columns.append(col)
            for col in phone_columns:
                if phone_last10(row.get(col)) in phone_set:
                    match_columns.append(col)
            row["matched_role"] = role_display_name(role_scope, staff.get("team"))
            row["matched_role_columns"] = match_columns
            out.append(compact_dict(row))
        return out

    def collect_caretaker_assigned_buildings(self, staff: dict[str, Any]) -> list[dict[str, Any]]:
        return self.collect_assigned_buildings(staff, "caretaker")

    def _current_bookings_by_prop(self, prop_ids: Sequence[Any]) -> dict[str, dict[str, Any]]:
        table_name = "staging_booking_confirm"
        if not prop_ids or not self.table_exists(table_name):
            return {}
        columns = self.table_columns(table_name)
        required = {"prop_id", "travel_from_date", "travel_to_date"}
        if not required.issubset(columns):
            return {}

        prop_id_values = [str(v) for v in prop_ids if v not in (None, "")]
        in_sql, in_params = build_in_params(prop_id_values, "prop")
        select_columns = [
            "source_id", "booking_id", "user_id", "lead_id", "prop_id", "booking_status", "host_confirm_status",
            "refund_status", "no_show_status", "booking_type", "booking_datetime", "travel_from_date",
            "travel_to_date", "check_in_time", "check_out_time", "synced_at",
        ]
        select_list = self.select_exprs(table_name, "bc", select_columns)
        booking_status_expr = "LOWER(COALESCE(bc.booking_status::text, ''))"
        booking_datetime_order = "bc.booking_datetime DESC NULLS LAST," if "booking_datetime" in columns else ""

        rows = self.rows(
            f"""
            WITH ranked AS (
                SELECT
                    {select_list},
                    ROW_NUMBER() OVER (
                        PARTITION BY bc.prop_id::text
                        ORDER BY
                            CASE
                                WHEN {booking_status_expr} = 'success'
                                 AND bc.travel_from_date::date <= CURRENT_DATE
                                 AND bc.travel_to_date::date >= CURRENT_DATE THEN 1
                                WHEN {booking_status_expr} = 'success'
                                 AND bc.travel_from_date::date > CURRENT_DATE THEN 2
                                ELSE 9
                            END,
                            bc.travel_from_date DESC NULLS LAST,
                            {booking_datetime_order}
                            bc.source_id DESC NULLS LAST
                    ) AS rn
                FROM {table_ref(self.schema, table_name)} bc
                WHERE bc.prop_id::text IN {in_sql}
            )
            SELECT *
            FROM ranked
            WHERE rn = 1
            """,
            in_params,
        )

        booking_ids = [str(row.get("source_id") or row.get("booking_id")) for row in rows if row.get("source_id") or row.get("booking_id")]
        checked_out = self._checkout_booking_ids(booking_ids)

        out: dict[str, dict[str, Any]] = {}
        today = now_ist_naive().date()
        for row in rows:
            prop_id = str(row.get("prop_id")) if row.get("prop_id") not in (None, "") else None
            if not prop_id:
                continue
            booking_key = str(row.get("source_id") or row.get("booking_id") or "")
            status = lower_ref(row.get("booking_status")) or ""
            from_date = coerce_date(row.get("travel_from_date"))
            to_date = coerce_date(row.get("travel_to_date"))
            has_checkout = booking_key in checked_out or str(row.get("booking_id") or "") in checked_out

            if not row.get("source_id"):
                occupancy_status = "vacant_no_booking"
            elif status != "success":
                occupancy_status = "vacant_non_success_booking"
            elif has_checkout:
                occupancy_status = "vacant_checked_out"
            elif from_date and to_date and from_date <= today <= to_date:
                occupancy_status = "occupied"
            elif from_date and from_date > today:
                occupancy_status = "upcoming_booking"
            elif to_date and to_date < today:
                occupancy_status = "vacant_past_booking"
            else:
                occupancy_status = "unknown"

            row["occupancy_status"] = occupancy_status
            row["has_checkout_evidence"] = has_checkout
            out[prop_id] = compact_dict(row)
        return out

    def _checkout_booking_ids(self, booking_ids: Sequence[str]) -> set[str]:
        table_name = "staging_checkout_form"
        if not booking_ids or not self.table_exists(table_name) or "booking_id" not in self.table_columns(table_name):
            return set()
        in_sql, in_params = build_in_params([str(v) for v in booking_ids if v], "bid")
        rows = self.rows(
            f"""
            SELECT DISTINCT booking_id::text AS booking_id
            FROM {table_ref(self.schema, table_name)}
            WHERE booking_id::text IN {in_sql}
            """,
            in_params,
        )
        return {str(row.get("booking_id")) for row in rows if row.get("booking_id") not in (None, "")}

    def collect_assigned_properties(self, buildings: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        table_name = "staging_property_unit"
        building_table = "staging_buildings"
        if not buildings or not self.table_exists(table_name):
            return []
        building_ids = [row.get("building_id") or row.get("source_id") for row in buildings if row.get("building_id") or row.get("source_id")]
        if not building_ids:
            return []
        in_sql, in_params = build_in_params([str(v) for v in building_ids], "bid")

        p_columns = [
            "source_id", "prop_id", "building_id", "unit_name", "unit_number", "unit_type", "rms_prop",
            "furnishing_type", "furnish_date", "rms_rent", "rms_maintain", "rms_deposit", "check_out",
            "bookable", "future_booking_days", "active", "verified", "active_search", "available_from_date",
            "listing_title", "display_property_name", "bedrooms", "beds", "bathrooms", "max_guests",
            "colive_type", "unit_area", "unit_age", "facing", "prop_floor", "prop_type_id", "room_type_id",
            "mark_check_out", "mark_electricity_bill", "mark_rent_paid", "asset_verified", "asset_verified_by",
            "asset_verified_on", "flat_verified", "flat_verified_by", "flat_verified_on", "added_on", "inactive_on",
            "last_updated_by", "last_updated_on", "synced_at",
        ]
        b_columns = ["building_name", "city", "area", "address", "pincode", "glat", "glng", "direction_note", "caretaker", "supervisor", "ops_manager", "sales"]
        select_list = self.select_exprs(table_name, "p", p_columns)
        if self.table_exists(building_table):
            select_list += ",\n               " + self.select_exprs(building_table, "b", b_columns, prefix="building_")
            join_sql = f"LEFT JOIN {table_ref(self.schema, building_table)} b ON b.building_id::text = p.building_id::text"
            order_sql = "LOWER(COALESCE(b.building_name::text, '')), p.unit_number NULLS LAST, p.prop_id NULLS LAST"
        else:
            select_list += ",\n               " + ",\n               ".join(f"NULL AS building_{_safe_ident(c)}" for c in b_columns)
            join_sql = ""
            order_sql = "p.building_id NULLS LAST, p.unit_number NULLS LAST, p.prop_id NULLS LAST"

        rms_filter = "AND COALESCE(p.rms_prop::text, '') = 'RMS Prop'" if "rms_prop" in self.table_columns(table_name) else ""
        rows = self.rows(
            f"""
            SELECT {select_list}
            FROM {table_ref(self.schema, table_name)} p
            {join_sql}
            WHERE p.building_id::text IN {in_sql}
              {rms_filter}
            ORDER BY {order_sql}
            """,
            in_params,
        )
        prop_ids = [row.get("prop_id") or row.get("source_id") for row in rows if row.get("prop_id") or row.get("source_id")]
        current_bookings = self._current_bookings_by_prop(prop_ids)

        out: list[dict[str, Any]] = []
        for row in rows:
            prop_id = row.get("prop_id") or row.get("source_id")
            booking = current_bookings.get(str(prop_id)) if prop_id not in (None, "") else None
            occupancy_status = (booking or {}).get("occupancy_status") or "vacant_no_current_booking"
            row["current_booking"] = booking
            row["occupancy_status"] = occupancy_status
            row["is_occupied_now"] = occupancy_status == "occupied"
            row["is_vacant_now"] = occupancy_status != "occupied"
            row["property"] = property_unit_display(row)
            row["property_unit_name"] = row["property"]
            out.append(compact_dict(row))
        return out

    def collect_caretaker_assigned_properties(self, buildings: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return self.collect_assigned_properties(buildings)

    # ------------------------------------------------------------------
    # Property/unit-name enrichment helpers
    # ------------------------------------------------------------------
    def _property_unit_info_by_prop_id(self, prop_ids: Sequence[Any]) -> dict[str, dict[str, Any]]:
        """Map prop_id/source_id to the standard staging_property_unit.unit_name label."""
        table_name = "staging_property_unit"
        keys = sorted({str(v) for v in prop_ids if v not in (None, "")})
        if not keys or not self.table_exists(table_name):
            return {}
        columns = self.table_columns(table_name)
        if not ({"prop_id", "source_id"} & columns):
            return {}

        in_sql, in_params = build_in_params(keys, "unitprop")
        conds: list[str] = []
        if "prop_id" in columns:
            conds.append(f"p.prop_id::text IN {in_sql}")
        if "source_id" in columns:
            conds.append(f"p.source_id::text IN {in_sql}")
        if not conds:
            return {}

        select_columns = [
            "source_id", "prop_id", "building_id", "unit_name", "unit_number", "unit_type",
            "display_property_name", "listing_title", "rms_prop", "active", "bookable",
        ]
        select_list = self.select_exprs(table_name, "p", select_columns)
        rows = self.rows(
            f"""
            SELECT {select_list}
            FROM {table_ref(self.schema, table_name)} p
            WHERE {' OR '.join(f'({cond})' for cond in conds)}
            """,
            in_params,
        )

        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            label = property_unit_display(row)
            info = compact_dict(
                {
                    "property": label,
                    "unit_name": label,
                    "prop_id": row.get("prop_id"),
                    "source_id": row.get("source_id"),
                    "building_id": row.get("building_id"),
                    "unit_type": row.get("unit_type"),
                    "rms_prop": row.get("rms_prop"),
                    "active": row.get("active"),
                    "bookable": row.get("bookable"),
                }
            )
            for key in (row.get("prop_id"), row.get("source_id")):
                if key not in (None, ""):
                    out[str(key)] = info
        return out

    def _property_unit_info_by_booking_id(self, booking_ids: Sequence[Any]) -> dict[str, dict[str, Any]]:
        """Map booking_id/source_id to unit_name through staging_booking_confirm.prop_id."""
        booking_table = "staging_booking_confirm"
        property_table = "staging_property_unit"
        keys = sorted({str(v) for v in booking_ids if v not in (None, "")})
        if not keys or not self.table_exists(booking_table):
            return {}
        booking_columns = self.table_columns(booking_table)
        if not ({"booking_id", "source_id"} & booking_columns) or "prop_id" not in booking_columns:
            return {}

        in_sql, in_params = build_in_params(keys, "unitbook")
        booking_conds: list[str] = []
        if "booking_id" in booking_columns:
            booking_conds.append(f"bc.booking_id::text IN {in_sql}")
        if "source_id" in booking_columns:
            booking_conds.append(f"bc.source_id::text IN {in_sql}")

        join_sql = ""
        unit_select = "NULL AS property_unit_name, NULL AS property_unit_type"
        property_columns = self.table_columns(property_table) if self.table_exists(property_table) else set()
        if property_columns:
            join_conds: list[str] = []
            if "prop_id" in property_columns:
                join_conds.append("p.prop_id::text = bc.prop_id::text")
            if "source_id" in property_columns:
                join_conds.append("p.source_id::text = bc.prop_id::text")
            if join_conds:
                join_sql = f"LEFT JOIN {table_ref(self.schema, property_table)} p ON {' OR '.join(f'({cond})' for cond in join_conds)}"
                unit_name_expr = "p.unit_name" if "unit_name" in property_columns else "NULL"
                unit_type_expr = "p.unit_type" if "unit_type" in property_columns else "NULL"
                unit_select = f"{unit_name_expr} AS property_unit_name, {unit_type_expr} AS property_unit_type"

        rows = self.rows(
            f"""
            SELECT
                bc.source_id AS booking_source_id,
                {('bc.booking_id' if 'booking_id' in booking_columns else 'NULL')} AS booking_id,
                bc.prop_id AS prop_id,
                {unit_select}
            FROM {table_ref(self.schema, booking_table)} bc
            {join_sql}
            WHERE {' OR '.join(f'({cond})' for cond in booking_conds)}
            """,
            in_params,
        )

        out: dict[str, dict[str, Any]] = {}
        prop_ids = [row.get("prop_id") for row in rows if row.get("prop_id") not in (None, "")]
        prop_map = self._property_unit_info_by_prop_id(prop_ids)
        for row in rows:
            prop_info = prop_map.get(str(row.get("prop_id"))) if row.get("prop_id") not in (None, "") else None
            label = clean_text(row.get("property_unit_name"), 160) or (prop_info or {}).get("unit_name") or property_unit_display(row)
            info = compact_dict(
                {
                    "property": label,
                    "unit_name": label,
                    "prop_id": row.get("prop_id"),
                    "unit_type": row.get("property_unit_type") or (prop_info or {}).get("unit_type"),
                }
            )
            for key in (row.get("booking_id"), row.get("booking_source_id")):
                if key not in (None, ""):
                    out[str(key)] = info
        return out

    def enrich_rows_with_unit_names(
        self,
        rows: Sequence[dict[str, Any]],
        *,
        prop_keys: Sequence[str] = ("prop_id",),
        booking_keys: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Mutate rows with property/unit_name for compact evidence views.

        The original IDs are intentionally left in raw rows for joins/debug, but
        evidence/LLM display should use `property`/`unit_name`.
        """
        if not rows:
            return list(rows)

        prop_ids: set[str] = set()
        booking_ids: set[str] = set()
        for row in rows:
            for key in prop_keys:
                value = row.get(key)
                if value not in (None, ""):
                    prop_ids.add(str(value))
            for key in booking_keys:
                value = row.get(key)
                if value not in (None, ""):
                    booking_ids.add(str(value))

        prop_map = self._property_unit_info_by_prop_id(sorted(prop_ids)) if prop_ids else {}
        booking_map = self._property_unit_info_by_booking_id(sorted(booking_ids)) if booking_ids else {}

        enriched: list[dict[str, Any]] = []
        for row in rows:
            info: dict[str, Any] = {}
            for key in prop_keys:
                value = row.get(key)
                if value not in (None, ""):
                    info = prop_map.get(str(value)) or info
                    if info:
                        break
            if not info:
                for key in booking_keys:
                    value = row.get(key)
                    if value not in (None, ""):
                        info = booking_map.get(str(value)) or info
                        if info:
                            break

            label = (info or {}).get("unit_name") or property_unit_display(row)
            if label:
                row["property"] = label
                row["unit_name"] = label
                row["property_unit_name"] = label
            if info.get("unit_type") and not row.get("unit_type"):
                row["unit_type"] = info.get("unit_type")
            if info.get("prop_id") and not row.get("prop_id"):
                row["prop_id"] = info.get("prop_id")
            enriched.append(row)
        return enriched

__all__ = [
    "DEFAULT_SCHEMA", "IST_OFFSET", "ACTIVE_TICKET_STATUSES", "CLOSED_TICKET_STATUSES",
    "ROLE_CONFIG", "ROLE_ALIASES", "StaffActivityBaseService",
    "normalize_role_scope", "role_display_name", "schema_ident", "table_ref", "now_ist_naive",
    "clean_text", "norm_digits", "phone_last10", "show_phone", "norm_email", "lower_ref",
    "coerce_datetime", "coerce_date", "fmt_dt", "fmt_duration", "build_in_params",
    "compact_dict", "avg_numeric", "direction_flow", "boolish", "property_unit_display",
    "account_type_display", "staff_admin_role_display", "staff_is_admin_display",
]
