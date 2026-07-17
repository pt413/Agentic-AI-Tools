from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session


SCHEMA = os.getenv("SCHEMA_NAME", "AnalyticsEngine")
DEFAULT_DAYS = 7
ALL_FACT_ENTITIES = ("lead", "booking", "ticket", "invoice", "property")
IST_OFFSET = timedelta(hours=5, minutes=30)
NON_DIGIT = re.compile(r"\D+")
EMAIL_SPLIT_RE = re.compile(r"[;,]")

COMPANY_TO_CUSTOMER_FLOWS = {"outgoing", "outbound", "sent", "reply", "from_admin", "dialed", "dial", "out"}
CUSTOMER_TO_COMPANY_FLOWS = {"incoming", "inbound", "received", "from_customer", "missed", "receive", "in"}


# -----------------------------------------------------------------------------
# SQL helpers
# -----------------------------------------------------------------------------

def q(db: Session, sql: str, params: Optional[dict] = None) -> List[Dict[str, Any]]:
    rows = db.execute(text(sql), params or {}).fetchall()
    return [dict(row._mapping) for row in rows]


def qs(db: Session, sql: str, params: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    rows = q(db, sql, params)
    return rows[0] if rows else None


# -----------------------------------------------------------------------------
# Small public helpers used by CustomerBriefService
# -----------------------------------------------------------------------------

def _now_local() -> datetime:
    return datetime.utcnow() + IST_OFFSET


def _today_local() -> date:
    return _now_local().date()


def match_phone(value: Optional[str]) -> Optional[str]:
    if value in (None, ""):
        return None
    digits = NON_DIGIT.sub("", str(value))
    if len(digits) >= 10:
        return digits[-10:]
    return None


def show_phone(value: Optional[str]) -> Optional[str]:
    last10 = match_phone(value)
    return f"91{last10}" if last10 else None


def norm_email(value: Optional[str]) -> Optional[str]:
    if value in (None, ""):
        return None
    email = str(value).strip().lower()
    return email if "@" in email else None


def build_in_params(values: Sequence[Any], prefix: str = "p") -> Tuple[str, Dict[str, Any]]:
    params: Dict[str, Any] = {}
    holders: List[str] = []
    seen: Set[str] = set()
    for value in values:
        if value in (None, ""):
            continue
        key_value = str(value)
        if key_value in seen:
            continue
        seen.add(key_value)
        key = f"{prefix}{len(holders)}"
        holders.append(f":{key}")
        params[key] = value
    return ("(" + ", ".join(holders) + ")", params) if holders else ("(NULL)", {})


def compact_inner_dict(value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    out: Dict[str, Any] = {}
    for key, item in value.items():
        if item in (None, "", [], {}, ()):  # keep zero/False
            continue
        if isinstance(item, dict):
            cleaned = compact_inner_dict(item)
            if cleaned not in (None, "", [], {}, ()):  # type: ignore[comparison-overlap]
                out[key] = cleaned
        elif isinstance(item, list):
            cleaned_list = []
            for child in item:
                if isinstance(child, dict):
                    cleaned_child = compact_inner_dict(child)
                    if cleaned_child not in (None, "", [], {}, ()):  # type: ignore[comparison-overlap]
                        cleaned_list.append(cleaned_child)
                elif child not in (None, "", [], {}, ()):  # type: ignore[comparison-overlap]
                    cleaned_list.append(child)
            if cleaned_list:
                out[key] = cleaned_list
        else:
            out[key] = item
    return out


def normalize_direction(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return str(value).strip().lower() or None


def summarize_text(value: Optional[str], max_length: int = 280) -> Optional[str]:
    if value is None:
        return None
    text_value = re.sub(r"\s+", " ", str(value)).strip()
    if not text_value:
        return None
    if len(text_value) <= max_length:
        return text_value
    return text_value[: max_length - 3].rstrip() + "..."


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
    text_value = str(value).strip()
    if not text_value:
        return None
    parsed = coerce_datetime(text_value)
    if parsed:
        return parsed.date()
    try:
        return date.fromisoformat(text_value[:10])
    except Exception:
        return None


def max_datetime(*values: Any) -> Optional[datetime]:
    candidates = [coerce_datetime(value) for value in values]
    candidates = [value for value in candidates if value is not None]
    return max(candidates) if candidates else None


def _safe_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _phone_expr(column_name: str) -> str:
    return f"RIGHT(REGEXP_REPLACE(COALESCE({column_name}::text,''),'\\D','','g'),10)"


class Universe:
    __slots__ = ("person_ids", "user_ids", "lead_ids", "booking_ids", "phones", "emails")

    def __init__(self) -> None:
        self.person_ids: Set[int] = set()
        self.user_ids: Set[int] = set()
        self.lead_ids: Set[int] = set()
        self.booking_ids: Set[int] = set()
        self.phones: Set[str] = set()   # last 10 digits only
        self.emails: Set[str] = set()

    def add_phone(self, value: Any) -> None:
        normalized = match_phone(None if value is None else str(value))
        if normalized:
            self.phones.add(normalized)

    def add_email(self, value: Any) -> None:
        normalized = norm_email(None if value is None else str(value))
        if normalized:
            self.emails.add(normalized)

    def add_int(self, attr: str, value: Any) -> None:
        number = _safe_int(value)
        if number is not None:
            getattr(self, attr).add(number)

    def snapshot(self) -> Tuple[frozenset, frozenset, frozenset, frozenset, frozenset, frozenset]:
        return (
            frozenset(self.person_ids),
            frozenset(self.user_ids),
            frozenset(self.lead_ids),
            frozenset(self.booking_ids),
            frozenset(self.phones),
            frozenset(self.emails),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "person_ids": sorted(self.person_ids),
            "user_ids": sorted(self.user_ids),
            "lead_ids": sorted(self.lead_ids),
            "booking_ids": sorted(self.booking_ids),
            "phones": [show_phone(phone) for phone in sorted(self.phones) if show_phone(phone)],
            "emails": sorted(self.emails),
        }


@dataclass
class MultipleSeedsError(Exception):
    message: str
    candidates: List[str]


class TimelineAccessService:
    def __init__(self, db: Session, schema: str = SCHEMA):
        self.db = db
        self.schema = schema
        self._table_exists_cache: Dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Generic utilities
    # ------------------------------------------------------------------
    def table_exists(self, table_name: str, schema_name: Optional[str] = None) -> bool:
        schema_name = schema_name or self.schema
        cache_key = f"{schema_name}.{table_name}"
        if cache_key not in self._table_exists_cache:
            row = qs(
                self.db,
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = :schema_name
                      AND table_name = :table_name
                ) AS present
                """,
                {"schema_name": schema_name, "table_name": table_name},
            )
            self._table_exists_cache[cache_key] = bool(row and row.get("present"))
        return self._table_exists_cache[cache_key]


    def table_columns(self, table_name: str, schema_name: Optional[str] = None) -> Set[str]:
        schema_name = schema_name or self.schema
        cache_key = f"{schema_name}.{table_name}:columns"
        cached = self._table_exists_cache.get(cache_key)
        if isinstance(cached, set):
            return cached

        if not self.table_exists(table_name, schema_name):
            self._table_exists_cache[cache_key] = set()
            return set()

        rows = q(
            self.db,
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = :schema_name
              AND table_name = :table_name
            """,
            {"schema_name": schema_name, "table_name": table_name},
        )
        columns = {str(row.get("column_name")) for row in rows if row.get("column_name")}
        self._table_exists_cache[cache_key] = columns
        return columns

    def existing_columns(self, table_name: str, candidates: Sequence[str], schema_name: Optional[str] = None) -> List[str]:
        columns = self.table_columns(table_name, schema_name)
        return [column for column in candidates if column in columns]

    def _ensure_single_seed(self, **kwargs: Any) -> None:
        active = [name for name, value in kwargs.items() if value not in (None, "")]
        if not active:
            raise ValueError("Provide exactly one of user_id, booking_id, lead_id, person_id, email, or phone.")
        if len(active) != 1:
            raise MultipleSeedsError("Multiple identifiers were provided. Please select exactly one and retry.", active)

    def _parse_entities(self, entities: str = "all") -> List[str]:
        raw = str(entities or "all").strip().lower()
        if raw == "all":
            return list(ALL_FACT_ENTITIES)
        parts = [part.strip().lower() for part in raw.split(",") if part.strip()]
        invalid = [part for part in parts if part not in ALL_FACT_ENTITIES]
        if not parts or invalid:
            allowed = ", ".join(ALL_FACT_ENTITIES)
            raise ValueError(f"entities must be 'all' or a comma-separated subset of: {allowed}")
        return list(dict.fromkeys(parts))

    def _window_from_days(self, days: int = DEFAULT_DAYS) -> Tuple[datetime, datetime]:
        safe_days = max(1, int(days or DEFAULT_DAYS))
        end_dt = _now_local()
        return end_dt - timedelta(days=safe_days), end_dt

    # ------------------------------------------------------------------
    # Identity/universe resolution
    # ------------------------------------------------------------------
    def _lookup_person_ids_by_contacts(self, emails: Sequence[str], phones: Sequence[str]) -> List[int]:
        if not self.table_exists("identity_person_key"):
            return []
        conds: List[str] = []
        params: Dict[str, Any] = {}
        emails = [email for email in emails if norm_email(email)]
        phones = [str(phone)[-10:] for phone in phones if match_phone(str(phone))]
        if emails:
            in_sql, in_params = build_in_params(sorted(set(emails)), "em")
            conds.append(f"(key_type = 'email' AND LOWER(COALESCE(key_value,'')) IN {in_sql})")
            params.update(in_params)
        if phones:
            in_sql, in_params = build_in_params(sorted(set(phones)), "ph")
            conds.append(
                f"(key_type IN ('phone','mobile','whatsapp_number') AND {_phone_expr('key_value')} IN {in_sql})"
            )
            params.update(in_params)
        if not conds:
            return []
        rows = q(
            self.db,
            f'''SELECT DISTINCT person_id FROM "{self.schema}".identity_person_key WHERE {' OR '.join(conds)} ORDER BY person_id''',
            params,
        )
        return [int(row["person_id"]) for row in rows if row.get("person_id") is not None]

    def _seed_universe(self, *, user_id=None, booking_id=None, lead_id=None, person_id=None, email=None, phone=None) -> Universe:
        universe = Universe()
        universe.add_int("user_ids", user_id)
        universe.add_int("booking_ids", booking_id)
        universe.add_int("lead_ids", lead_id)
        universe.add_int("person_ids", person_id)
        universe.add_email(email)
        universe.add_phone(phone)
        for pid in self._lookup_person_ids_by_contacts(list(universe.emails), list(universe.phones)):
            universe.person_ids.add(int(pid))
        return universe

    def _expand_from_identity_person_key(self, universe: Universe) -> None:
        if not self.table_exists("identity_person_key"):
            return
        conds: List[str] = []
        params: Dict[str, Any] = {}
        if universe.person_ids:
            conds.append("person_id = ANY(:pids)")
            params["pids"] = list(universe.person_ids)
        if universe.user_ids:
            conds.append("(key_type = 'user_id' AND key_value::text = ANY(:uids))")
            params["uids"] = [str(x) for x in universe.user_ids]
        if universe.lead_ids:
            conds.append("(key_type = 'lead_id' AND key_value::text = ANY(:lids))")
            params["lids"] = [str(x) for x in universe.lead_ids]
        if universe.booking_ids:
            conds.append("(key_type = 'booking_id' AND key_value::text = ANY(:bids))")
            params["bids"] = [str(x) for x in universe.booking_ids]
        if universe.emails:
            conds.append("(key_type = 'email' AND LOWER(COALESCE(key_value,'')) = ANY(:emails))")
            params["emails"] = list(universe.emails)
        if universe.phones:
            conds.append(f"(key_type IN ('phone','mobile','whatsapp_number') AND {_phone_expr('key_value')} = ANY(:phones))")
            params["phones"] = list(universe.phones)
        if not conds:
            return
        rows = q(
            self.db,
            f'''SELECT person_id, key_type, key_value, source_table, source_id
                FROM "{self.schema}".identity_person_key
                WHERE {' OR '.join(f'({c})' for c in conds)}''',
            params,
        )
        for row in rows:
            universe.add_int("person_ids", row.get("person_id"))
            key_type = str(row.get("key_type") or "").lower()
            key_value = row.get("key_value")
            if key_type == "user_id":
                universe.add_int("user_ids", key_value)
            elif key_type == "lead_id":
                universe.add_int("lead_ids", key_value)
            elif key_type == "booking_id":
                universe.add_int("booking_ids", key_value)
            elif key_type in {"phone", "mobile", "whatsapp_number"}:
                universe.add_phone(key_value)
            elif key_type == "email":
                universe.add_email(key_value)

    def _expand_from_user_account(self, universe: Universe) -> None:
        if not self.table_exists("staging_user_account"):
            return
        conds: List[str] = []
        params: Dict[str, Any] = {}
        if universe.user_ids:
            conds.append("source_id::text = ANY(:uids)")
            params["uids"] = [str(x) for x in universe.user_ids]
        if universe.emails:
            conds.append("LOWER(COALESCE(email,'')) = ANY(:emails)")
            params["emails"] = list(universe.emails)
        if universe.phones:
            conds.append(
                f"({_phone_expr('normalized_phone')} = ANY(:phones) OR {_phone_expr('phone_number')} = ANY(:phones))"
            )
            params["phones"] = list(universe.phones)
        if not conds:
            return
        rows = q(
            self.db,
            f'''SELECT source_id, email, normalized_phone, phone_number
                FROM "{self.schema}".staging_user_account
                WHERE {' OR '.join(f'({c})' for c in conds)}''',
            params,
        )
        for row in rows:
            universe.add_int("user_ids", row.get("source_id"))
            universe.add_email(row.get("email"))
            universe.add_phone(row.get("normalized_phone"))
            universe.add_phone(row.get("phone_number"))

    def _expand_from_user_contact_info(self, universe: Universe) -> None:
        if not self.table_exists("staging_user_contact_info"):
            return
        conds: List[str] = []
        params: Dict[str, Any] = {}
        if universe.booking_ids:
            conds.append("booking_id::text = ANY(:bids)")
            params["bids"] = [str(x) for x in universe.booking_ids]
        if universe.user_ids:
            conds.append("user_id::text = ANY(:uids)")
            params["uids"] = [str(x) for x in universe.user_ids]
        if universe.emails:
            conds.append("LOWER(COALESCE(email,'')) = ANY(:emails)")
            params["emails"] = list(universe.emails)
        if universe.phones:
            conds.append(f"({_phone_expr('normalized_mobile')} = ANY(:phones) OR {_phone_expr('mobile')} = ANY(:phones))")
            params["phones"] = list(universe.phones)
        if not conds:
            return
        rows = q(
            self.db,
            f'''SELECT source_id, user_id, booking_id, email, mobile, normalized_mobile
                FROM "{self.schema}".staging_user_contact_info
                WHERE {' OR '.join(f'({c})' for c in conds)}''',
            params,
        )
        for row in rows:
            universe.add_int("user_ids", row.get("user_id"))
            universe.add_int("booking_ids", row.get("booking_id"))
            universe.add_email(row.get("email"))
            universe.add_phone(row.get("normalized_mobile"))
            universe.add_phone(row.get("mobile"))

    def _expand_from_leads(self, universe: Universe) -> None:
        table_name = "staging_lead_tracking"
        if not self.table_exists(table_name):
            return

        cols = self.table_columns(table_name)
        if not cols:
            return

        def has(column: str) -> bool:
            return column in cols

        conds: List[str] = []
        params: Dict[str, Any] = {}

        if universe.lead_ids and has("source_id"):
            conds.append("source_id::text = ANY(:lids)")
            params["lids"] = [str(x) for x in universe.lead_ids]
        if universe.user_ids and has("user_id"):
            conds.append("user_id::text = ANY(:uids)")
            params["uids"] = [str(x) for x in universe.user_ids]
        if universe.booking_ids and has("booking_id"):
            conds.append("booking_id::text = ANY(:bids)")
            params["bids"] = [str(x) for x in universe.booking_ids]

        email_cols = self.existing_columns(table_name, ["email", "email_id", "user_email", "customer_email"])
        if universe.emails and email_cols:
            email_conditions = [f"LOWER(COALESCE({col}::text,'')) = ANY(:emails)" for col in email_cols]
            conds.append("(" + " OR ".join(email_conditions) + ")")
            params["emails"] = list(universe.emails)

        phone_cols = self.existing_columns(
            table_name,
            [
                "contact_number",
                "contact_number_alt",
                "contact_details",
                "contact_details2",
                "phone",
                "mobile",
                "normalized_phone",
            ],
        )
        if universe.phones and phone_cols:
            phone_conditions = [f"{_phone_expr(col)} = ANY(:phones)" for col in phone_cols]
            conds.append("(" + " OR ".join(phone_conditions) + ")")
            params["phones"] = list(universe.phones)

        if not conds:
            return

        select_cols = self.existing_columns(
            table_name,
            [
                "source_id",
                "user_id",
                "booking_id",
                "person_id",
                *email_cols,
                *phone_cols,
            ],
        )
        if not select_cols:
            return

        rows = q(
            self.db,
            f'''SELECT {', '.join(select_cols)}
                FROM "{self.schema}".{table_name}
                WHERE {' OR '.join(f'({c})' for c in conds)}''',
            params,
        )
        for row in rows:
            universe.add_int("lead_ids", row.get("source_id"))
            universe.add_int("user_ids", row.get("user_id"))
            universe.add_int("booking_ids", row.get("booking_id"))
            universe.add_int("person_ids", row.get("person_id"))
            for col in email_cols:
                universe.add_email(row.get(col))
            for col in phone_cols:
                universe.add_phone(row.get(col))

    def _expand_from_bookings(self, universe: Universe) -> None:
        if not self.table_exists("staging_booking_confirm"):
            return
        conds: List[str] = []
        params: Dict[str, Any] = {}
        if universe.booking_ids:
            conds.append("(source_id::text = ANY(:bids) OR booking_id::text = ANY(:bids))")
            params["bids"] = [str(x) for x in universe.booking_ids]
        if universe.user_ids:
            conds.append("user_id::text = ANY(:uids)")
            params["uids"] = [str(x) for x in universe.user_ids]
        if universe.lead_ids:
            conds.append("lead_id::text = ANY(:lids)")
            params["lids"] = [str(x) for x in universe.lead_ids]
        if not conds:
            return
        rows = q(
            self.db,
            f'''SELECT source_id, booking_id, user_id, lead_id
                FROM "{self.schema}".staging_booking_confirm
                WHERE {' OR '.join(f'({c})' for c in conds)}''',
            params,
        )
        for row in rows:
            universe.add_int("booking_ids", row.get("source_id"))
            universe.add_int("booking_ids", row.get("booking_id"))
            universe.add_int("user_ids", row.get("user_id"))
            universe.add_int("lead_ids", row.get("lead_id"))

    def resolve_universe(
        self,
        *,
        user_id: Optional[int] = None,
        booking_id: Optional[int] = None,
        lead_id: Optional[int] = None,
        person_id: Optional[int] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_single_seed(
            user_id=user_id,
            booking_id=booking_id,
            lead_id=lead_id,
            person_id=person_id,
            email=email,
            phone=phone,
        )
        universe = self._seed_universe(
            user_id=user_id,
            booking_id=booking_id,
            lead_id=lead_id,
            person_id=person_id,
            email=email,
            phone=phone,
        )
        for _ in range(4):
            before = universe.snapshot()
            self._expand_from_identity_person_key(universe)
            self._expand_from_user_account(universe)
            self._expand_from_user_contact_info(universe)
            self._expand_from_leads(universe)
            self._expand_from_bookings(universe)
            if universe.snapshot() == before:
                break
        return universe.as_dict()

    # ------------------------------------------------------------------
    # Linked IDs and fact rows
    # ------------------------------------------------------------------
    def _booking_contact_candidates(self, universe: Dict[str, Any]) -> Dict[str, List[str]]:
        out = {"emails": [], "phones": []}
        booking_ids = [str(x) for x in (universe.get("booking_ids") or [])]
        if not booking_ids or not self.table_exists("staging_user_contact_info"):
            return out
        in_sql, in_params = build_in_params(booking_ids, "ucbid")
        rows = q(
            self.db,
            f'''SELECT DISTINCT email, mobile, normalized_mobile
                FROM "{self.schema}".staging_user_contact_info
                WHERE booking_id::text IN {in_sql}''',
            in_params,
        )
        seen_emails: Set[str] = set()
        seen_phones: Set[str] = set()
        for row in rows:
            email = norm_email(row.get("email"))
            if email and email not in seen_emails:
                seen_emails.add(email)
                out["emails"].append(email)
            for key in ("normalized_mobile", "mobile"):
                phone = match_phone(row.get(key))
                if phone and phone not in seen_phones:
                    seen_phones.add(phone)
                    out["phones"].append(phone)
        return out

    def _collect_linked_ticket_ids(self, universe: Dict[str, Any]) -> List[Any]:
        if not self.table_exists("staging_user_ticket"):
            return []
        conds: List[str] = []
        params: Dict[str, Any] = {}
        booking_ids = [str(x) for x in (universe.get("booking_ids") or [])]
        phone_last10 = [str(p)[-10:] for p in (universe.get("phones") or [])]
        if booking_ids:
            conds.append("booking_id::text = ANY(:bids)")
            params["bids"] = booking_ids
        if phone_last10:
            conds.append(f"{_phone_expr('mobile_number')} = ANY(:phones)")
            params["phones"] = phone_last10
        if not conds:
            return []
        rows = q(
            self.db,
            f'''SELECT source_id FROM "{self.schema}".staging_user_ticket
                WHERE {' OR '.join(f'({c})' for c in conds)}
                ORDER BY COALESCE(close_date, created_at, synced_at) DESC NULLS LAST, source_id DESC''',
            params,
        )
        return [row["source_id"] for row in rows if row.get("source_id") is not None]

    def _collect_linked_invoice_ids(self, universe: Dict[str, Any]) -> List[Any]:
        if not self.table_exists("staging_booking_invoice_details"):
            return []
        booking_ids = [str(x) for x in (universe.get("booking_ids") or [])]
        if not booking_ids:
            return []
        rows = q(
            self.db,
            f'''SELECT source_id FROM "{self.schema}".staging_booking_invoice_details
                WHERE booking_id::text = ANY(:bids)
                ORDER BY COALESCE(utr_added_on, send_time, created_on, synced_at) DESC NULLS LAST, source_id DESC''',
            {"bids": booking_ids},
        )
        return [row["source_id"] for row in rows if row.get("source_id") is not None]

    def _lead_rows(self, universe: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self.table_exists("staging_lead_tracking"):
            return []
        conds: List[str] = []
        params: Dict[str, Any] = {}
        if universe.get("lead_ids"):
            conds.append("source_id::text = ANY(:lids)")
            params["lids"] = [str(x) for x in universe["lead_ids"]]
        if universe.get("user_ids"):
            conds.append("user_id::text = ANY(:uids)")
            params["uids"] = [str(x) for x in universe["user_ids"]]
        if universe.get("booking_ids"):
            conds.append("booking_id::text = ANY(:bids)")
            params["bids"] = [str(x) for x in universe["booking_ids"]]
        if universe.get("emails"):
            conds.append("LOWER(COALESCE(email,'')) = ANY(:emails)")
            params["emails"] = list(universe["emails"])
        phones = [str(p)[-10:] for p in (universe.get("phones") or [])]
        if phones:
            conds.append(f"({_phone_expr('contact_number')} = ANY(:phones) OR {_phone_expr('contact_number_alt')} = ANY(:phones))")
            params["phones"] = phones
        if not conds:
            return []
        return q(
            self.db,
            f'''SELECT source_id, user_id, booking_id, executive_id, created_at, closed_at,
                       raw_status, contact_number, contact_number_alt, email, assigned_to, synced_at
                FROM "{self.schema}".staging_lead_tracking
                WHERE {' OR '.join(f'({c})' for c in conds)}
                ORDER BY COALESCE(created_at, synced_at) DESC NULLS LAST, source_id DESC''',
            params,
        )

    def _booking_rows(self, universe: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self.table_exists("staging_booking_confirm"):
            return []
        conds: List[str] = []
        params: Dict[str, Any] = {}
        if universe.get("booking_ids"):
            conds.append("(source_id::text = ANY(:bids) OR booking_id::text = ANY(:bids))")
            params["bids"] = [str(x) for x in universe["booking_ids"]]
        if universe.get("user_ids"):
            conds.append("user_id::text = ANY(:uids)")
            params["uids"] = [str(x) for x in universe["user_ids"]]
        if universe.get("lead_ids"):
            conds.append("lead_id::text = ANY(:lids)")
            params["lids"] = [str(x) for x in universe["lead_ids"]]
        if not conds:
            return []
        return q(
            self.db,
            f'''SELECT source_id, booking_id, user_id, lead_id, prop_id,
                       booking_status, host_confirm_status, refund_status, no_show_status,
                       booking_type, period, booking_datetime, travel_from_date,
                       travel_to_date, nights, total_amount, total_after_discount,
                       amount_paid, advance_amount, paid_advance_amount,
                       check_in_time, check_out_time, synced_at
                FROM "{self.schema}".staging_booking_confirm
                WHERE {' OR '.join(f'({c})' for c in conds)}
                ORDER BY COALESCE(booking_datetime, synced_at) DESC NULLS LAST, source_id DESC''',
            params,
        )

    def _ticket_rows(self, universe: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self.table_exists("staging_user_ticket"):
            return []
        conds: List[str] = []
        params: Dict[str, Any] = {}
        booking_ids = [str(x) for x in (universe.get("booking_ids") or [])]
        phones = [str(p)[-10:] for p in (universe.get("phones") or [])]
        if booking_ids:
            conds.append("booking_id::text = ANY(:bids)")
            params["bids"] = booking_ids
        if phones:
            conds.append(f"{_phone_expr('mobile_number')} = ANY(:phones)")
            params["phones"] = phones
        if not conds:
            return []
        return q(
            self.db,
            f'''SELECT source_id, booking_id, prop_id, building_id, building_name,
                       category, priority, description, mobile_number, unit_number,
                       status, reopen_flag, created_at, assigned_to, resolved_by,
                       closed_by, close_date, active_days, synced_at
                FROM "{self.schema}".staging_user_ticket
                WHERE {' OR '.join(f'({c})' for c in conds)}
                ORDER BY COALESCE(close_date, created_at, synced_at) DESC NULLS LAST, source_id DESC''',
            params,
        )

    def _invoice_rows(self, universe: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self.table_exists("staging_booking_invoice_details"):
            return []
        booking_ids = [str(x) for x in (universe.get("booking_ids") or [])]
        if not booking_ids:
            return []
        return q(
            self.db,
            f'''SELECT source_id, booking_id, amount_status, duration_period,
                       mail_status, sa_mail_status, reminder_mail, amount_recieved,
                       amount, total_amount, from_date, till_date, pending_balance,
                       comment, status, mail_count, send_time, transaction_type,
                       created_on, synced_at
                FROM "{self.schema}".staging_booking_invoice_details
                WHERE booking_id::text = ANY(:bids)
                ORDER BY COALESCE(send_time, created_on, synced_at) DESC NULLS LAST, source_id DESC''',
            {"bids": booking_ids},
        )

    def _site_visit_rows(self, universe: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self.table_exists("staging_site_visits") or not universe.get("lead_ids"):
            return []
        return q(
            self.db,
            f'''SELECT source_id, lead_id, building_id, prop_id, site_visit_date, synced_at
                FROM "{self.schema}".staging_site_visits
                WHERE lead_id::text = ANY(:lids)
                ORDER BY COALESCE(site_visit_date, synced_at) DESC NULLS LAST, source_id DESC''',
            {"lids": [str(x) for x in universe["lead_ids"]]},
        )

    # ------------------------------------------------------------------
    # Business state / formatting helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _is_active_lead_status(value: Optional[str]) -> bool:
        return str(value or "").strip().lower() in {"waiting", "active"}

    @staticmethod
    def _is_active_ticket_status(value: Optional[str]) -> bool:
        return str(value or "").strip().lower() in {"open", "reopen", "waiting", "pending", "assigned", "new", "in progress"}

    def _pick_active_lead(self, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        candidates = [row for row in rows if self._is_active_lead_status(row.get("raw_status"))]
        return candidates[0] if candidates else (rows[0] if rows else None)

    def _find_checkout_form_evidence(self, booking_row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.table_exists("staging_checkout_form"):
            return None
        booking_ids = [str(booking_row.get(key)) for key in ("source_id", "booking_id") if booking_row.get(key) not in (None, "")]
        if not booking_ids:
            return None
        in_sql, in_params = build_in_params(sorted(set(booking_ids)), "chkbid")
        row = qs(
            self.db,
            f'''SELECT booking_id, checkout_date, added_time, synced_at
                FROM "{self.schema}".staging_checkout_form
                WHERE booking_id::text IN {in_sql}
                ORDER BY COALESCE(checkout_date, added_time, synced_at) DESC NULLS LAST
                LIMIT 1''',
            in_params,
        )
        if not row:
            return None
        event_time = max_datetime(row.get("checkout_date"), row.get("added_time"), row.get("synced_at"))
        return {"source": "checkout_form", "event_time": event_time, "checkout_date": coerce_date(event_time), "confidence": "high"}

    def _derive_booking_state(self, booking_row: Dict[str, Any], universe: Dict[str, Any], today: date) -> Dict[str, Any]:
        row = dict(booking_row)
        scheduled_start = coerce_date(row.get("travel_from_date"))
        scheduled_end = coerce_date(row.get("travel_to_date"))
        status = str(row.get("booking_status") or "").strip().lower()
        checkout_evidence = self._find_checkout_form_evidence(row)
        actual_checkout_date = coerce_date(checkout_evidence.get("checkout_date")) if checkout_evidence else None
        actual_checkout_time = coerce_datetime(checkout_evidence.get("event_time")) if checkout_evidence else None
        quality_flags: List[str] = []

        if status != "success":
            is_active = False
            current_state = "inactive_non_success"
            state_reason = f"booking_status={status or 'unknown'}"
        elif actual_checkout_date:
            is_active = False
            current_state = "checkout_completed_early" if scheduled_end and actual_checkout_date < scheduled_end else "checkout_completed"
            state_reason = f"checkout evidence on {actual_checkout_date.isoformat()}"
            if current_state == "checkout_completed_early":
                quality_flags.append("actual_checkout_before_scheduled_end")
        elif scheduled_end and scheduled_end < today:
            is_active = False
            current_state = "stay_completed"
            state_reason = f"scheduled stay ended on {scheduled_end.isoformat()}"
        elif scheduled_start and scheduled_end and scheduled_start <= today <= scheduled_end:
            is_active = True
            current_state = "active_stay"
            state_reason = f"today {today.isoformat()} is within scheduled stay"
        elif scheduled_start and today < scheduled_start:
            is_active = True
            current_state = "upcoming_active_booking"
            state_reason = f"future stay starts on {scheduled_start.isoformat()}"
        else:
            is_active = status == "success"
            current_state = "active_booking_open" if is_active else "unknown"
            state_reason = "success booking without contradictory checkout evidence" if is_active else None

        row.update(
            {
                "derived_is_active": is_active,
                "current_state": current_state,
                "state_reason": state_reason,
                "scheduled_end_date": scheduled_end,
                "actual_checkout_date": actual_checkout_date,
                "actual_checkout_time": actual_checkout_time,
                "checkout_evidence_source": checkout_evidence.get("source") if checkout_evidence else None,
                "last_updated_at": max_datetime(
                    row.get("synced_at"), row.get("booking_datetime"), row.get("check_in_time"), row.get("check_out_time"), actual_checkout_time
                ),
                "quality_flags": quality_flags,
            }
        )
        return row

    def _pick_active_booking(self, rows: List[Dict[str, Any]], today: date, universe: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        universe = universe or {}
        derived = [self._derive_booking_state(row, universe, today) for row in rows]
        active = [row for row in derived if row.get("derived_is_active")]
        if not active:
            return derived[0] if derived else None

        def current_key(row: Dict[str, Any]) -> Tuple[int, date, datetime]:
            start = coerce_date(row.get("travel_from_date")) or date.min
            end = coerce_date(row.get("travel_to_date")) or date.max
            is_current = int(start <= today <= end)
            booked_at = coerce_datetime(row.get("booking_datetime")) or datetime.min
            return is_current, start, booked_at

        active.sort(key=current_key, reverse=True)
        return active[0]

    def _pick_active_tickets(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [row for row in rows if self._is_active_ticket_status(row.get("status"))]

    def _pick_active_invoices(self, rows: List[Dict[str, Any]], active_booking: Optional[Dict[str, Any]], today: date) -> List[Dict[str, Any]]:
        if not active_booking:
            return []
        booking_ids = {str(active_booking.get("source_id")), str(active_booking.get("booking_id"))}
        booking_ids.discard("None")
        out = []
        for row in rows:
            if str(row.get("booking_id")) not in booking_ids:
                continue
            if str(row.get("amount_status") or "").strip().lower() != "pending":
                continue
            pivot = coerce_date(row.get("from_date") or row.get("created_on"))
            if pivot and pivot > today:
                continue
            out.append(row)
        return out

    def _format_lead(self, row: Optional[Dict[str, Any]], fallback_lead_id: Optional[Any] = None) -> Optional[Dict[str, Any]]:
        if not row and fallback_lead_id in (None, ""):
            return None
        flags = ["identity_linked_but_lead_fact_missing"] if not row and fallback_lead_id not in (None, "") else []
        return compact_inner_dict(
            {
                "lead_id": row.get("source_id") if row else fallback_lead_id,
                "created_at": row.get("created_at") if row else None,
                "closed_at": row.get("closed_at") if row else None,
                "raw_status": row.get("raw_status") if row else None,
                "contact_number": show_phone(row.get("contact_number")) if row else None,
                "contact_number_alt": show_phone(row.get("contact_number_alt")) if row else None,
                "email": row.get("email") if row else None,
                "assigned_to": row.get("assigned_to") if row else None,
                "freshness": {"last_updated_at": max_datetime(row.get("synced_at"), row.get("closed_at"), row.get("created_at"))} if row else None,
                "quality_flags": flags,
            }
        )

    def _format_booking(self, row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        travel_to = coerce_date(row.get("travel_to_date"))
        return compact_inner_dict(
            {
                "booking_id": row.get("source_id") or row.get("booking_id"),
                "booking_status": row.get("booking_status"),
                "status": row.get("booking_status"),
                "host_confirm_status": row.get("host_confirm_status"),
                "refund_status": row.get("refund_status"),
                "no_show_status": row.get("no_show_status"),
                "booking_type": row.get("booking_type"),
                "type": row.get("booking_type"),
                "property_id": row.get("prop_id"),
                "period": row.get("period"),
                "booking_datetime": row.get("booking_datetime"),
                "travel_from_date": row.get("travel_from_date"),
                "travel_to_date": row.get("travel_to_date"),
                "scheduled_end_date": row.get("scheduled_end_date"),
                "days_until_booked_end": (travel_to - _today_local()).days if travel_to else None,
                "nights": row.get("nights"),
                "total_amount": row.get("total_amount"),
                "discounted_total": row.get("total_after_discount"),
                "amount_paid": row.get("amount_paid"),
                "advance_amount": row.get("advance_amount"),
                "paid_advance_amount": row.get("paid_advance_amount"),
                "check_in_time": row.get("check_in_time"),
                "check_out_time": row.get("check_out_time"),
                "is_active": row.get("derived_is_active"),
                "current_state": row.get("current_state"),
                "state_reason": row.get("state_reason"),
                "actual_checkout_date": row.get("actual_checkout_date"),
                "actual_checkout_time": row.get("actual_checkout_time"),
                "checkout_evidence_source": row.get("checkout_evidence_source"),
                "freshness": {"last_updated_at": row.get("last_updated_at")} if row.get("last_updated_at") else None,
                "quality_flags": row.get("quality_flags") or [],
            }
        )

    def _format_ticket(self, row: Dict[str, Any]) -> Dict[str, Any]:
        assigned_to = row.get("assigned_to")
        if str(assigned_to or "").strip().lower() == "unassigned":
            assigned_to = None
        return compact_inner_dict(
            {
                "ticket_id": row.get("source_id"),
                "category": row.get("category"),
                "priority": row.get("priority"),
                "description": row.get("description"),
                "mobile_number": show_phone(row.get("mobile_number")),
                "status": row.get("status"),
                "created_at": row.get("created_at"),
                "assigned_to": assigned_to,
                "resolved_by": row.get("resolved_by"),
                "closed_by": row.get("closed_by"),
                "close_date": row.get("close_date"),
                "active_days": row.get("active_days"),
            }
        ) or {}

    def _format_invoice(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return compact_inner_dict(
            {
                "invoice_id": row.get("source_id"),
                "amount_status": row.get("amount_status"),
                "duration_period": row.get("duration_period"),
                "mail_status": row.get("mail_status"),
                "reminder_mail": row.get("reminder_mail"),
                "amount_recieved": row.get("amount_recieved"),
                "amount": row.get("amount"),
                "total_amount": row.get("total_amount"),
                "from_date": row.get("from_date"),
                "till_date": row.get("till_date"),
                "pending_balance": row.get("pending_balance"),
                "comment": row.get("comment"),
                "status": row.get("status"),
                "send_time": row.get("send_time"),
                "transaction_type": row.get("transaction_type"),
                "created_on": row.get("created_on"),
            }
        ) or {}

    def _format_property(self, active_booking: Optional[Dict[str, Any]], ticket_rows: List[Dict[str, Any]], booking_rows: List[Dict[str, Any]], site_visit_rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        property_id = (active_booking or {}).get("prop_id") or (booking_rows[0].get("prop_id") if booking_rows else None)
        building_id = self._lookup_property_building_id(property_id)
        for row in ticket_rows:
            if property_id is None or str(row.get("prop_id")) == str(property_id):
                building_id = row.get("building_id") or building_id
                if building_id:
                    break
        if not building_id and site_visit_rows:
            building_id = site_visit_rows[0].get("building_id")
        if not property_id and site_visit_rows:
            property_id = site_visit_rows[0].get("prop_id")
        if property_id in (None, "") and building_id in (None, ""):
            return None
        return compact_inner_dict({"property_id": property_id, "building": self._lookup_building(building_id)})

    def _lookup_property_building_id(self, property_id: Any) -> Optional[Any]:
        if property_id in (None, "") or not self.table_exists("staging_property_unit"):
            return None
        row = qs(
            self.db,
            f"""
            SELECT building_id
            FROM "{self.schema}".staging_property_unit
            WHERE prop_id::text = :pid OR source_id::text = :pid
            ORDER BY COALESCE(last_updated_on, prop_info_last_update_time, added_on) DESC NULLS LAST
            LIMIT 1
            """,
            {"pid": str(property_id)},
        )
        return row.get("building_id") if row else None

    def _lookup_building(self, building_id: Any) -> Optional[Dict[str, Any]]:
        if building_id in (None, "") or not self.table_exists("staging_buildings"):
            return None
        row = qs(
            self.db,
            f"""
            SELECT
                building_id, building_name, city, area, address, pincode,
                glat, glng, direction_note, caretaker, supervisor, ops_manager,
                finance_supervisor, sales, sales_phone, building_status,
                rent_model, updated_on
            FROM "{self.schema}".staging_buildings
            WHERE building_id::text = :bid OR source_id::text = :bid
            ORDER BY updated_on DESC NULLS LAST
            LIMIT 1
            """,
            {"bid": str(building_id)},
        )
        if not row:
            return None
        return compact_inner_dict(
            {
                "building_id": row.get("building_id"),
                "building_name": row.get("building_name"),
                "city": row.get("city"),
                "area": row.get("area"),
                "address": row.get("address"),
                "pincode": row.get("pincode"),
                "location": compact_inner_dict({"lat": row.get("glat"), "lng": row.get("glng")}),
                "direction_note": row.get("direction_note"),
                "caretaker": row.get("caretaker"),
                "supervisor": row.get("supervisor"),
                "ops_manager": row.get("ops_manager"),
                "finance_supervisor": row.get("finance_supervisor"),
                "sales": row.get("sales"),
                "sales_phone": show_phone(row.get("sales_phone")),
                "building_status": row.get("building_status"),
                "rent_model": row.get("rent_model"),
                "freshness": {"last_updated_at": row.get("updated_on")},
            }
        )

    # ------------------------------------------------------------------
    # Conversation fetchers
    # ------------------------------------------------------------------
    def _conversation_phone_scope(self, universe: Dict[str, Any]) -> List[str]:
        phones = {str(p)[-10:] for p in (universe.get("phones") or []) if match_phone(str(p))}
        for phone in self._booking_contact_candidates(universe).get("phones") or []:
            if match_phone(phone):
                phones.add(str(phone)[-10:])
        return sorted(phones)

    def _fetch_whatsapp_messages(self, universe: Dict[str, Any], start_dt: datetime, end_dt: datetime) -> List[Dict[str, Any]]:
        if not self.table_exists("staging_whatsapp_messages"):
            return []
        conds: List[str] = []
        params: Dict[str, Any] = {"start_dt": start_dt, "end_dt": end_dt}
        phones = self._conversation_phone_scope(universe)
        if phones:
            conds.append(f"({_phone_expr('cx_number')} = ANY(:phones) OR {_phone_expr('admin_number')} = ANY(:phones))")
            params["phones"] = phones
        if universe.get("lead_ids"):
            conds.append("lead_id::text = ANY(:lids)")
            params["lids"] = [str(x) for x in universe["lead_ids"]]
        if not conds:
            return []
        rows = q(
            self.db,
            f'''SELECT message_time, direction, message_type, clean_content, admin_number, cx_number
                FROM "{self.schema}".staging_whatsapp_messages
                WHERE ({' OR '.join(f'({c})' for c in conds)})
                  AND message_time >= :start_dt
                  AND message_time < :end_dt
                ORDER BY message_time ASC NULLS LAST''',
            params,
        )
        out: List[Dict[str, Any]] = []
        for row in rows:
            direction = normalize_direction(row.get("direction"))
            company_to_customer = direction in COMPANY_TO_CUSTOMER_FLOWS
            out.append(
                compact_inner_dict(
                    {
                        "time": row.get("message_time"),
                        "role_flow": "company>customer" if company_to_customer else "customer>company",
                        "text": summarize_text(row.get("clean_content")),
                    }
                ) or {}
            )
        return out

    def _fetch_call_messages(self, universe: Dict[str, Any], start_dt: datetime, end_dt: datetime) -> List[Dict[str, Any]]:
        call_table = "staging_call_recordings_transcript"
        if not self.table_exists(call_table):
            return []
        conds: List[str] = []
        params: Dict[str, Any] = {"start_dt": start_dt, "end_dt": end_dt}
        phones = self._conversation_phone_scope(universe)
        if phones:
            conds.append(f"({_phone_expr('counterparty_phone')} = ANY(:phones) OR {_phone_expr('sales_phone')} = ANY(:phones))")
            params["phones"] = phones
        if universe.get("lead_ids"):
            conds.append("lead_id::text = ANY(:lids)")
            params["lids"] = [str(x) for x in universe["lead_ids"]]
        if not conds:
            return []
        rows = q(
            self.db,
            f"""SELECT source_id, lead_id, executive_id, executive_name, call_time, talk_time_sec,
                       call_direction, call_result, counterparty_phone, sales_phone,
                       translated_text, transcript_text, transcript_text_eleven_labs, raw_transcripts,
                       intent, emotion, tone, action_layer, context, outcome, language, audio_url
                FROM "{self.schema}".{call_table}
                WHERE ({' OR '.join(f'({c})' for c in conds)})
                  AND call_time >= :start_dt
                  AND call_time < :end_dt
                ORDER BY call_time ASC NULLS LAST, source_id ASC""",
            params,
        )
        out: List[Dict[str, Any]] = []
        for row in rows:
            direction = normalize_direction(row.get("call_direction")) or "unknown"
            duration = _safe_int(row.get("talk_time_sec")) or 0
            status = normalize_direction(row.get("call_result")) or "unknown"
            transcript = summarize_text(
                row.get("translated_text")
                or row.get("transcript_text")
                or row.get("transcript_text_eleven_labs")
                or row.get("raw_transcripts"),
                600,
            )
            call_summary = f"{direction} {status} call" + (f" ({duration}s)" if duration else "")
            out.append(
                compact_inner_dict(
                    {
                        "time": row.get("call_time"),
                        "role_flow": "company>customer" if direction in COMPANY_TO_CUSTOMER_FLOWS else "customer>company",
                        "event_type": "call",
                        "status": status,
                        "direction": direction,
                        "duration_sec": duration,
                        "agent": row.get("executive_id") or row.get("executive_name"),
                        "customer_phone": show_phone(row.get("counterparty_phone")),
                        "business_phone": show_phone(row.get("sales_phone")),
                        "text": f"{call_summary} | Transcript: {transcript}" if transcript else call_summary,
                        "transcript": transcript,
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

    def _email_scope(self, universe: Dict[str, Any]) -> List[str]:
        emails = {str(e).lower() for e in (universe.get("emails") or []) if norm_email(e)}
        for email in self._booking_contact_candidates(universe).get("emails") or []:
            emails.add(email)
        booking_ids = [str(x) for x in (universe.get("booking_ids") or [])]
        for table_name, column in (("staging_checkin_form", "user_email"), ("staging_checkout_form", "user_email")):
            if not booking_ids or not self.table_exists(table_name):
                continue
            in_sql, in_params = build_in_params(booking_ids, f"{table_name[:3]}bid")
            rows = q(
                self.db,
                f'''SELECT DISTINCT {column} AS email FROM "{self.schema}".{table_name}
                    WHERE booking_id::text IN {in_sql} AND COALESCE({column}, '') <> '' ''',
                in_params,
            )
            for row in rows:
                email = norm_email(row.get("email"))
                if email:
                    emails.add(email)
        return sorted(emails)

    def _infer_email_role_flow(self, sender: Optional[str], receiver: Optional[str], customer_emails: Sequence[str]) -> str:
        sender_norm = norm_email(sender)
        customer_set = {email for email in customer_emails if norm_email(email)}
        receiver_values = [norm_email(v) for v in EMAIL_SPLIT_RE.split(str(receiver or "")) if norm_email(v)]
        if sender_norm in customer_set:
            return "customer>company"
        if any(value in customer_set for value in receiver_values):
            return "company>customer"
        return "company>customer"

    def _fetch_email_messages(self, universe: Dict[str, Any], start_dt: datetime, end_dt: datetime) -> List[Dict[str, Any]]:
        if not self.table_exists("staging_email_messages"):
            return []
        emails = self._email_scope(universe)
        if not emails:
            return []
        in_sql, in_params = build_in_params(emails, "em")
        values_sql = ",".join(f"(:em{i})" for i in range(len(emails)))
        rows = q(
            self.db,
            f'''
            WITH matched AS (
                SELECT
                    source_id,
                    email_date,
                    sender,
                    receiver,
                    subject,
                    body,
                    snippet,
                    direction
                FROM "{self.schema}".staging_email_messages
                WHERE (
                        LOWER(COALESCE(sender, '')) IN {in_sql}
                     OR EXISTS (
                            SELECT 1 FROM (VALUES {values_sql}) AS e(email)
                            WHERE LOWER(COALESCE(receiver, '')) LIKE '%' || e.email || '%'
                        )
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
            ''',
            {**in_params, "start_dt": start_dt, "end_dt": end_dt},
        )
        out: List[Dict[str, Any]] = []
        for row in rows:
            out.append(
                compact_inner_dict(
                    {
                        "time": row.get("email_date"),
                        "role_flow": self._infer_email_role_flow(row.get("sender"), row.get("receiver"), emails),
                        "subject": row.get("subject"),
                        "text": summarize_text(row.get("snippet") or row.get("body") or row.get("subject")),
                    }
                ) or {}
            )
        return out

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def identity(self, *, user_id=None, booking_id=None, lead_id=None, person_id=None, email=None, phone=None) -> Dict[str, Any]:
        universe = self.resolve_universe(
            user_id=user_id,
            booking_id=booking_id,
            lead_id=lead_id,
            person_id=person_id,
            email=email,
            phone=phone,
        )
        universe["ticket_ids"] = self._collect_linked_ticket_ids(universe)
        universe["invoice_ids"] = self._collect_linked_invoice_ids(universe)
        return universe

    def facts(self, *, mode: str = "active", entities: str = "all", user_id=None, booking_id=None, lead_id=None, person_id=None, email=None, phone=None) -> Dict[str, Any]:
        normalized_mode = str(mode or "active").strip().lower()
        if normalized_mode not in {"active", "all"}:
            raise ValueError("mode must be 'active' or 'all'.")
        requested = self._parse_entities(entities)
        universe = self.resolve_universe(
            user_id=user_id,
            booking_id=booking_id,
            lead_id=lead_id,
            person_id=person_id,
            email=email,
            phone=phone,
        )
        today = _today_local()
        need_booking = any(entity in requested for entity in ("booking", "invoice", "property"))
        need_ticket = any(entity in requested for entity in ("ticket", "property"))

        lead_rows = self._lead_rows(universe) if "lead" in requested else []
        booking_rows = self._booking_rows(universe) if need_booking else []
        ticket_rows = self._ticket_rows(universe) if need_ticket else []
        invoice_rows = self._invoice_rows(universe) if "invoice" in requested else []
        site_visit_rows = self._site_visit_rows(universe) if "property" in requested else []

        active_lead = self._pick_active_lead(lead_rows)
        active_booking = self._pick_active_booking(booking_rows, today, universe=universe)
        active_tickets = self._pick_active_tickets(ticket_rows)
        active_invoices = self._pick_active_invoices(invoice_rows, active_booking, today)

        payload: Dict[str, Any] = {}
        if "lead" in requested:
            fallback = sorted(universe.get("lead_ids") or [None])[0] if normalized_mode == "active" else None
            payload["lead"] = self._format_lead(active_lead, fallback) if normalized_mode == "active" else [self._format_lead(row) for row in lead_rows]
        if "booking" in requested:
            payload["booking"] = self._format_booking(active_booking) if normalized_mode == "active" else [self._format_booking(self._derive_booking_state(row, universe, today)) for row in booking_rows]
        if "ticket" in requested:
            payload["ticket"] = [self._format_ticket(row) for row in (active_tickets if normalized_mode == "active" else ticket_rows)]
        if "invoice" in requested:
            payload["invoice"] = [self._format_invoice(row) for row in (active_invoices if normalized_mode == "active" else invoice_rows)]
        if "property" in requested:
            payload["property_details"] = self._format_property(active_booking, ticket_rows, booking_rows, site_visit_rows)

        payload["freshness"] = compact_inner_dict(
            {
                "generated_at": _now_local(),
                "lead_last_updated_at": max_datetime(*([row.get("synced_at") for row in lead_rows] or [None])),
                "booking_last_updated_at": max_datetime(*([row.get("last_updated_at") or row.get("synced_at") for row in ([active_booking] if active_booking else booking_rows)] or [None])),
                "ticket_last_updated_at": max_datetime(*([row.get("synced_at") for row in ticket_rows] or [None])),
                "invoice_last_updated_at": max_datetime(*([row.get("synced_at") or row.get("send_time") or row.get("created_on") for row in invoice_rows] or [None])),
            }
        ) or {}
        quality_flags: List[str] = []
        for key in ("lead", "booking"):
            if isinstance(payload.get(key), dict):
                quality_flags.extend(payload[key].get("quality_flags") or [])
        payload["quality_flags"] = sorted(set(flag for flag in quality_flags if flag))
        return payload

    def conversation(self, *, channel: str = "any", days: int = DEFAULT_DAYS, user_id=None, booking_id=None, lead_id=None, person_id=None, email=None, phone=None) -> Dict[str, Any]:
        normalized_channel = str(channel or "any").strip().lower()
        if normalized_channel not in {"any", "whatsapp", "email", "call"}:
            raise ValueError("channel must be 'any', 'whatsapp', 'email', or 'call'.")
        universe = self.resolve_universe(
            user_id=user_id,
            booking_id=booking_id,
            lead_id=lead_id,
            person_id=person_id,
            email=email,
            phone=phone,
        )
        safe_days = max(1, int(days or DEFAULT_DAYS))
        start_dt, end_dt = self._window_from_days(safe_days)
        payload: Dict[str, Any] = {
            "channel": normalized_channel,
            "days": safe_days,
            "window": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        }
        if normalized_channel in {"any", "call"}:
            payload["call"] = {"messages": self._fetch_call_messages(universe, start_dt, end_dt)}
        if normalized_channel in {"any", "whatsapp"}:
            payload["whatsapp"] = {"messages": self._fetch_whatsapp_messages(universe, start_dt, end_dt)}
        if normalized_channel in {"any", "email"}:
            payload["email"] = {"messages": self._fetch_email_messages(universe, start_dt, end_dt)}

        all_messages: List[Dict[str, Any]] = []
        for channel_name in ("call", "whatsapp", "email"):
            for item in (payload.get(channel_name) or {}).get("messages", []):
                merged = dict(item)
                merged["channel"] = channel_name
                all_messages.append(merged)
        all_messages.sort(key=lambda item: item.get("time") or "")
        if normalized_channel == "any":
            payload["recent_message_count"] = len(all_messages)
            payload["recent_messages"] = all_messages
        payload["freshness"] = compact_inner_dict(
            {"generated_at": _now_local(), "last_message_at": max_datetime(*([item.get("time") for item in all_messages] or [None]))}
        ) or {}
        payload["quality_flags"] = ["contains_truncated_message_text"] if any(str(item.get("text") or "").endswith("...") for item in all_messages) else []
        return payload

