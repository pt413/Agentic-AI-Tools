from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import text
from sqlalchemy.orm import Session

try:
    from app.services.analytics_engine.capabilities.timeline_access_service import SCHEMA
except Exception:  # pragma: no cover
    SCHEMA = "AnalyticsEngine"


SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
IST_OFFSET = timedelta(hours=5, minutes=30)
CONTEXT_VERSION_IDS = "active_booking_recent_activity_ids:v5"
CONTEXT_VERSION_FULL = "active_booking_recent_activity:v4"
log = logging.getLogger(__name__)


def _now_local() -> datetime:
    return datetime.utcnow() + IST_OFFSET


def _safe_ident(value: str) -> str:
    if not SAFE_IDENT_RE.fullmatch(str(value or "")):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return str(value)


def _phone_expr(column_sql: str) -> str:
    return f"RIGHT(REGEXP_REPLACE(COALESCE({column_sql}::text, ''), '\\D', '', 'g'), 10)"


def _coerce_iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _compact_dict(value: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, item in (value or {}).items():
        if item in (None, "", [], {}, ()):  # keep 0 / False
            continue
        if isinstance(item, dict):
            child = _compact_dict(item)
            if child:
                out[key] = child
        elif isinstance(item, list):
            cleaned = []
            for row in item:
                if isinstance(row, dict):
                    child = _compact_dict(row)
                    if child:
                        cleaned.append(child)
                elif row not in (None, "", [], {}, ()):
                    cleaned.append(row)
            if cleaned:
                out[key] = cleaned
        else:
            out[key] = item
    return out


@dataclass
class ActiveBookingActivityService:
    """Fast tracker for active RMS bookings with recent activity.

    Uses staged temp tables and per-source timings. The default route can return
    only booking IDs, which avoids heavy event text/timeline payloads.
    """

    db: Session
    schema: str = SCHEMA
    _table_exists_cache: Dict[str, bool] = field(default_factory=dict, init=False, repr=False)
    _table_columns_cache: Dict[str, Set[str]] = field(default_factory=dict, init=False, repr=False)
    _debug_steps: List[Dict[str, Any]] = field(default_factory=list, init=False, repr=False)

    def _schema_ref(self) -> str:
        return f'"{_safe_ident(self.schema)}"'

    def _table_ref(self, table_name: str) -> str:
        return f"{self._schema_ref()}.{_safe_ident(table_name)}"

    def _rows(self, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        return [dict(row) for row in self.db.execute(text(sql), params or {}).mappings().fetchall()]

    def _scalar(self, sql: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.db.execute(text(sql), params or {}).scalar()

    def _exec(self, sql: str, params: Optional[Dict[str, Any]] = None) -> None:
        self.db.execute(text(sql), params or {})

    def _time_step(self, name: str, fn, *, debug: bool = False, **extra):
        started = time.perf_counter()
        status = "ok"
        row_count = None
        try:
            result = fn()
            if isinstance(result, list):
                row_count = len(result)
            elif isinstance(result, int):
                row_count = result
            elif isinstance(result, dict) and "row_count" in result:
                row_count = result.get("row_count")
            return result
        except Exception:
            status = "error"
            raise
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            record = {"step": name, "elapsed_ms": elapsed_ms, "status": status, **extra}
            if row_count is not None:
                record["row_count"] = row_count
            self._debug_steps.append(record)
            if debug:
                log.info("active-booking-activity %s", record)

    def table_exists(self, table_name: str) -> bool:
        cache_key = f"{self.schema}.{table_name}"
        if cache_key in self._table_exists_cache:
            return self._table_exists_cache[cache_key]
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
            {"schema_name": self.schema, "table_name": table_name},
        ).mappings().fetchone()
        present = bool(row and row.get("present"))
        self._table_exists_cache[cache_key] = present
        return present

    def table_columns(self, table_name: str) -> Set[str]:
        cache_key = f"{self.schema}.{table_name}"
        if cache_key in self._table_columns_cache:
            return self._table_columns_cache[cache_key]
        if not self.table_exists(table_name):
            self._table_columns_cache[cache_key] = set()
            return set()
        rows = self._rows(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = :schema_name
              AND table_name = :table_name
            """,
            {"schema_name": self.schema, "table_name": table_name},
        )
        columns = {str(row["column_name"]) for row in rows if row.get("column_name")}
        self._table_columns_cache[cache_key] = columns
        return columns

    def _require_columns(self, table_name: str, required: Set[str]) -> None:
        if not self.table_exists(table_name):
            raise ValueError(f"{table_name} table is required")
        missing = sorted(required - self.table_columns(table_name))
        if missing:
            raise ValueError(f"{table_name} missing required column(s): {', '.join(missing)}")

    # ------------------------------------------------------------------
    # Temp-table setup
    # ------------------------------------------------------------------
    def _create_active_bookings_temp(self, *, today: date) -> int:
        self._require_columns(
            "staging_booking_confirm",
            {"source_id", "prop_id", "booking_status", "travel_from_date", "travel_to_date"},
        )
        self._require_columns("staging_property_unit", {"prop_id", "unit_name", "rms_prop"})
        pu_cols = self.table_columns("staging_property_unit")
        order_col = "last_updated_on" if "last_updated_on" in pu_cols else "synced_at" if "synced_at" in pu_cols else "prop_id"
        building_select = "building_id" if "building_id" in pu_cols else "NULL::text AS building_id"
        b_cols = self.table_columns("staging_booking_confirm")
        booking_id_expr = "b.booking_id" if "booking_id" in b_cols else "NULL::bigint"
        user_id_expr = "b.user_id" if "user_id" in b_cols else "NULL::bigint"
        lead_id_expr = "b.lead_id" if "lead_id" in b_cols else "NULL::bigint"
        booking_type_expr = "b.booking_type" if "booking_type" in b_cols else "NULL::text"
        booking_dt_expr = "b.booking_datetime" if "booking_datetime" in b_cols else "NULL::timestamp"
        total_expr = "b.total_amount" if "total_amount" in b_cols else "NULL::numeric"

        self._exec("DROP TABLE IF EXISTS pg_temp.ae_active_bookings")
        self._exec(
            f"""
            CREATE TEMP TABLE ae_active_bookings ON COMMIT DROP AS
            WITH today_ctx AS (
                SELECT CAST(:today AS date) AS today
            ),
            property_unit AS (
                SELECT DISTINCT ON (prop_id::text)
                    prop_id::text AS prop_key,
                    unit_name::text AS property_name,
                    {building_select},
                    rms_prop::text AS rms_prop
                FROM {self._table_ref('staging_property_unit')}
                WHERE prop_id IS NOT NULL
                ORDER BY prop_id::text, {order_col} DESC NULLS LAST
            )
            SELECT
                b.source_id::bigint AS source_id,
                {booking_id_expr} AS booking_id,
                COALESCE({booking_id_expr}::text, b.source_id::text) AS booking_key,
                b.source_id::text AS source_id_text,
                {booking_id_expr}::text AS booking_id_text,
                {user_id_expr} AS user_id,
                {lead_id_expr} AS lead_id,
                b.prop_id AS prop_id,
                pu.property_name AS property_name,
                pu.building_id AS building_id,
                pu.rms_prop AS rms_prop,
                b.booking_status AS booking_status,
                {booking_type_expr} AS booking_type,
                {booking_dt_expr} AS booking_datetime,
                b.travel_from_date AS travel_from_date,
                b.travel_to_date AS travel_to_date,
                {total_expr} AS total_amount
            FROM {self._table_ref('staging_booking_confirm')} b
            CROSS JOIN today_ctx t
            LEFT JOIN property_unit pu
                ON pu.prop_key = b.prop_id::text
            WHERE LOWER(TRIM(COALESCE(b.booking_status::text, ''))) = 'success'
              AND LOWER(TRIM(COALESCE(pu.rms_prop::text, ''))) = 'rms prop'
              AND b.travel_from_date IS NOT NULL
              AND b.travel_to_date IS NOT NULL
              AND b.travel_from_date::date <= t.today
              AND b.travel_to_date::date >= t.today
            """,
            {"today": today},
        )
        self._exec("CREATE INDEX ON ae_active_bookings (booking_key)")
        self._exec("CREATE INDEX ON ae_active_bookings (source_id_text)")
        self._exec("CREATE INDEX ON ae_active_bookings (booking_id_text)")
        self._exec("CREATE INDEX ON ae_active_bookings (user_id)")
        self._exec("CREATE INDEX ON ae_active_bookings (lead_id)")
        return int(self._scalar("SELECT COUNT(*) FROM ae_active_bookings") or 0)

    def _create_phone_contacts_temp(self) -> int:
        selects: List[str] = []
        if self.table_exists("staging_user_account"):
            cols = self.table_columns("staging_user_account")
            phone_exprs = [f"ua.{col}" for col in ("normalized_phone", "phone_number") if col in cols]
            if "source_id" in cols and phone_exprs:
                selects.append(
                    f"""
                    SELECT ab.booking_key, {_phone_expr('COALESCE(' + ', '.join(phone_exprs) + ')')} AS phone10
                    FROM ae_active_bookings ab
                    JOIN {self._table_ref('staging_user_account')} ua
                      ON ua.source_id::text = ab.user_id::text
                    """
                )
        if self.table_exists("staging_user_contact_info"):
            cols = self.table_columns("staging_user_contact_info")
            phone_exprs = [f"ci.{col}" for col in ("normalized_mobile", "mobile") if col in cols]
            if "booking_id" in cols and phone_exprs:
                selects.append(
                    f"""
                    SELECT ab.booking_key, {_phone_expr('COALESCE(' + ', '.join(phone_exprs) + ')')} AS phone10
                    FROM ae_active_bookings ab
                    JOIN {self._table_ref('staging_user_contact_info')} ci
                      ON ci.booking_id::text IN (ab.booking_key, ab.source_id_text, ab.booking_id_text)
                    """
                )
        if self.table_exists("staging_lead_tracking"):
            cols = self.table_columns("staging_lead_tracking")
            join_parts = []
            if "booking_id" in cols:
                join_parts.append("lt.booking_id::text IN (ab.booking_key, ab.source_id_text, ab.booking_id_text)")
            if "source_id" in cols:
                join_parts.append("lt.source_id::text = ab.lead_id::text")
            for col in ("contact_number", "contact_number_alt"):
                if col in cols and join_parts:
                    selects.append(
                        f"""
                        SELECT ab.booking_key, {_phone_expr('lt.' + col)} AS phone10
                        FROM ae_active_bookings ab
                        JOIN {self._table_ref('staging_lead_tracking')} lt
                          ON ({' OR '.join(join_parts)})
                        """
                    )
        union_sql = "\nUNION ALL\n".join(selects) if selects else "SELECT NULL::text AS booking_key, NULL::text AS phone10 WHERE FALSE"
        self._exec("DROP TABLE IF EXISTS pg_temp.ae_booking_phone_contacts")
        self._exec(
            f"""
            CREATE TEMP TABLE ae_booking_phone_contacts ON COMMIT DROP AS
            SELECT DISTINCT booking_key, phone10
            FROM ({union_sql}) src
            WHERE phone10 IS NOT NULL AND LENGTH(phone10) = 10
            """
        )
        self._exec("CREATE INDEX ON ae_booking_phone_contacts (phone10)")
        self._exec("CREATE INDEX ON ae_booking_phone_contacts (booking_key)")
        return int(self._scalar("SELECT COUNT(*) FROM ae_booking_phone_contacts") or 0)

    def _create_email_contacts_temp(self) -> int:
        selects: List[str] = []
        if self.table_exists("staging_user_account"):
            cols = self.table_columns("staging_user_account")
            if {"source_id", "email"}.issubset(cols):
                selects.append(
                    f"""
                    SELECT ab.booking_key, LOWER(TRIM(ua.email::text)) AS email
                    FROM ae_active_bookings ab
                    JOIN {self._table_ref('staging_user_account')} ua
                      ON ua.source_id::text = ab.user_id::text
                    """
                )
        if self.table_exists("staging_user_contact_info"):
            cols = self.table_columns("staging_user_contact_info")
            if {"booking_id", "email"}.issubset(cols):
                selects.append(
                    f"""
                    SELECT ab.booking_key, LOWER(TRIM(ci.email::text)) AS email
                    FROM ae_active_bookings ab
                    JOIN {self._table_ref('staging_user_contact_info')} ci
                      ON ci.booking_id::text IN (ab.booking_key, ab.source_id_text, ab.booking_id_text)
                    """
                )
        if self.table_exists("staging_lead_tracking"):
            cols = self.table_columns("staging_lead_tracking")
            join_parts = []
            if "booking_id" in cols:
                join_parts.append("lt.booking_id::text IN (ab.booking_key, ab.source_id_text, ab.booking_id_text)")
            if "source_id" in cols:
                join_parts.append("lt.source_id::text = ab.lead_id::text")
            if "email" in cols and join_parts:
                selects.append(
                    f"""
                    SELECT ab.booking_key, LOWER(TRIM(lt.email::text)) AS email
                    FROM ae_active_bookings ab
                    JOIN {self._table_ref('staging_lead_tracking')} lt
                      ON ({' OR '.join(join_parts)})
                    """
                )
        union_sql = "\nUNION ALL\n".join(selects) if selects else "SELECT NULL::text AS booking_key, NULL::text AS email WHERE FALSE"
        self._exec("DROP TABLE IF EXISTS pg_temp.ae_booking_email_contacts")
        self._exec(
            f"""
            CREATE TEMP TABLE ae_booking_email_contacts ON COMMIT DROP AS
            SELECT DISTINCT booking_key, email
            FROM ({union_sql}) src
            WHERE email IS NOT NULL AND email <> '' AND email LIKE '%@%'
            """
        )
        self._exec("CREATE INDEX ON ae_booking_email_contacts (email)")
        self._exec("CREATE INDEX ON ae_booking_email_contacts (booking_key)")
        return int(self._scalar("SELECT COUNT(*) FROM ae_booking_email_contacts") or 0)

    # ------------------------------------------------------------------
    # Source scans
    # ------------------------------------------------------------------
    def _insert_activity_source(self, source_name: str, sql_body: str, params: Dict[str, Any]) -> int:
        before = int(self._scalar("SELECT COUNT(*) FROM ae_recent_booking_activity") or 0)
        self._exec(
            f"""
            INSERT INTO ae_recent_booking_activity (booking_key, source, last_activity_at, event_count)
            SELECT booking_key, :source_name AS source, MAX(event_time) AS last_activity_at, COUNT(*)::bigint AS event_count
            FROM ({sql_body}) src
            WHERE booking_key IS NOT NULL AND event_time IS NOT NULL
            GROUP BY booking_key
            """,
            {**params, "source_name": source_name},
        )
        after = int(self._scalar("SELECT COUNT(*) FROM ae_recent_booking_activity") or 0)
        return after - before

    def _scan_direct_sources(self, params: Dict[str, Any], *, debug: bool) -> None:
        self._exec("DROP TABLE IF EXISTS pg_temp.ae_recent_booking_activity")
        self._exec(
            """
            CREATE TEMP TABLE ae_recent_booking_activity (
                booking_key TEXT NOT NULL,
                source TEXT NOT NULL,
                last_activity_at TIMESTAMP NULL,
                event_count BIGINT NOT NULL DEFAULT 0
            ) ON COMMIT DROP
            """
        )

        def add_source(source_name: str, sql_body: Optional[str]) -> None:
            if not sql_body:
                return
            self._time_step(
                f"scan_{source_name}",
                lambda: self._insert_activity_source(source_name, sql_body, params),
                debug=debug,
            )

        if self.table_exists("staging_user_ticket"):
            cols = self.table_columns("staging_user_ticket")
            if "booking_id" in cols:
                time_cols = [f"t.{c}" for c in ("close_date", "created_at", "synced_at") if c in cols]
                time_expr = "COALESCE(" + ", ".join(time_cols) + ")" if time_cols else "NULL::timestamp"
                add_source(
                    "ticket",
                    f"""
                    SELECT ab.booking_key, {time_expr} AS event_time
                    FROM ae_active_bookings ab
                    JOIN {self._table_ref('staging_user_ticket')} t
                      ON t.booking_id::text IN (ab.booking_key, ab.source_id_text, ab.booking_id_text)
                    WHERE {time_expr} >= :start_dt AND {time_expr} < :end_dt
                    """,
                )
        if self.table_exists("staging_booking_audit_history"):
            cols = self.table_columns("staging_booking_audit_history")
            if {"booking_id", "added_time"}.issubset(cols):
                add_source(
                    "booking_audit",
                    f"""
                    SELECT ab.booking_key, a.added_time AS event_time
                    FROM ae_active_bookings ab
                    JOIN {self._table_ref('staging_booking_audit_history')} a
                      ON a.booking_id::text IN (ab.booking_key, ab.source_id_text, ab.booking_id_text)
                    WHERE a.added_time >= :start_dt AND a.added_time < :end_dt
                    """,
                )
        if self.table_exists("staging_checkin_form"):
            cols = self.table_columns("staging_checkin_form")
            if "booking_id" in cols:
                time_cols = [f"ci.{c}" for c in ("checkin_date", "added_on", "synced_at") if c in cols]
                time_expr = "COALESCE(" + ", ".join(time_cols) + ")" if time_cols else "NULL::timestamp"
                add_source(
                    "checkin_form",
                    f"""
                    SELECT ab.booking_key, {time_expr} AS event_time
                    FROM ae_active_bookings ab
                    JOIN {self._table_ref('staging_checkin_form')} ci
                      ON ci.booking_id::text IN (ab.booking_key, ab.source_id_text, ab.booking_id_text)
                    WHERE {time_expr} >= :start_dt AND {time_expr} < :end_dt
                    """,
                )
        if self.table_exists("staging_user_contact_info"):
            cols = self.table_columns("staging_user_contact_info")
            if {"booking_id", "added_on"}.issubset(cols):
                add_source(
                    "contact_info",
                    f"""
                    SELECT ab.booking_key, uci.added_on AS event_time
                    FROM ae_active_bookings ab
                    JOIN {self._table_ref('staging_user_contact_info')} uci
                      ON uci.booking_id::text IN (ab.booking_key, ab.source_id_text, ab.booking_id_text)
                    WHERE uci.added_on >= :start_dt AND uci.added_on < :end_dt
                    """,
                )
        # Do NOT count staging_booking_confirm.booking_datetime as activity.
        # staging_booking_confirm defines the active booking scope only.
        # Counting booking creation/sync as activity creates false positives,
        # e.g. a new active booking with zero ticket/conversation evidence.
        if self.table_exists("staging_site_visits"):
            cols = self.table_columns("staging_site_visits")
            if "lead_id" in cols:
                time_cols = [f"sv.{c}" for c in ("site_visit_date", "added_on", "synced_at") if c in cols]
                time_expr = "COALESCE(" + ", ".join(time_cols) + ")" if time_cols else "NULL::timestamp"
                add_source(
                    "site_visit",
                    f"""
                    SELECT ab.booking_key, {time_expr} AS event_time
                    FROM ae_active_bookings ab
                    JOIN {self._table_ref('staging_site_visits')} sv
                      ON sv.lead_id::text = ab.lead_id::text
                    WHERE {time_expr} >= :start_dt AND {time_expr} < :end_dt
                    """,
                )

    def _scan_communication_sources(self, params: Dict[str, Any], *, debug: bool, include_contact_scans: bool) -> None:
        if not include_contact_scans:
            return

        def add_source(source_name: str, sql_body: Optional[str], source_params: Optional[Dict[str, Any]] = None) -> None:
            if not sql_body:
                return
            self._time_step(
                f"scan_{source_name}",
                lambda: self._insert_activity_source(source_name, sql_body, {**params, **(source_params or {})}),
                debug=debug,
            )

        if self.table_exists("staging_call_log_unified"):
            cols = self.table_columns("staging_call_log_unified")
            if {"counterparty_phone", "call_time"}.issubset(cols):
                add_source(
                    "call",
                    f"""
                    SELECT pc.booking_key, c.call_time AS event_time
                    FROM ae_booking_phone_contacts pc
                    JOIN {self._table_ref('staging_call_log_unified')} c
                      ON {_phone_expr('c.counterparty_phone')} = pc.phone10
                    WHERE c.call_time >= :start_dt AND c.call_time < :end_dt
                    """,
                )
        if self.table_exists("staging_whatsapp_messages"):
            cols = self.table_columns("staging_whatsapp_messages")
            if {"cx_number", "message_time"}.issubset(cols):
                add_source(
                    "whatsapp",
                    f"""
                    SELECT pc.booking_key, w.message_time AS event_time
                    FROM ae_booking_phone_contacts pc
                    JOIN {self._table_ref('staging_whatsapp_messages')} w
                      ON {_phone_expr('w.cx_number')} = pc.phone10
                    WHERE w.message_time >= :start_dt AND w.message_time < :end_dt
                    """,
                )
        if self.table_exists("staging_email_messages"):
            cols = self.table_columns("staging_email_messages")
            if "email_date" in cols:
                sender_expr = "LOWER(TRIM(COALESCE(e.sender::text, '')))" if "sender" in cols else "''"
                receiver_expr = "LOWER(COALESCE(e.receiver::text, ''))" if "receiver" in cols else "''"
                add_source(
                    "email",
                    f"""
                    SELECT ec.booking_key, e.email_date AS event_time
                    FROM ae_booking_email_contacts ec
                    JOIN {self._table_ref('staging_email_messages')} e
                      ON {sender_expr} = ec.email
                      OR {receiver_expr} LIKE ('%' || ec.email || '%')
                    WHERE e.email_date >= :email_start_dt AND e.email_date < :email_end_dt
                    """,
                )

    def _final_ids(self, *, limit: int, offset: int = 0) -> List[Dict[str, Any]]:
        return self._rows(
            """
            SELECT
                ab.source_id AS booking_id,
                ab.booking_id AS raw_booking_id,
                MAX(ra.last_activity_at) AS last_activity_at,
                SUM(ra.event_count)::bigint AS event_count,
                ARRAY_AGG(DISTINCT ra.source ORDER BY ra.source) AS activity_sources
            FROM ae_recent_booking_activity ra
            JOIN ae_active_bookings ab ON ab.booking_key = ra.booking_key
            GROUP BY ab.source_id, ab.booking_id
            ORDER BY MAX(ra.last_activity_at) DESC NULLS LAST, ab.source_id DESC
            LIMIT :limit_n OFFSET :offset_n
            """,
            {
                "limit_n": int(limit),
                "offset_n": max(0, int(offset or 0)),
            },
        )
    
    def prepare_id_scan(self, *, days: int = 3, debug: bool = False, include_contact_scans: bool = False) -> Dict[str, Any]:
        self._debug_steps = []

        safe_days = max(1, min(int(days or 3), 365))
        end_dt = _now_local()
        start_dt = end_dt - timedelta(days=safe_days)
        today = end_dt.date()
        params = {
            "start_dt": start_dt,
            "end_dt": end_dt,
            "email_start_dt": start_dt,
            "email_end_dt": end_dt,
        }

        total_started = time.perf_counter()
        active_count = self._time_step(
            "create_active_bookings_temp",
            lambda: self._create_active_bookings_temp(today=today),
            debug=debug,
        )
        phone_contact_count = 0
        email_contact_count = 0

        if include_contact_scans:
            phone_contact_count = self._time_step(
                "create_phone_contacts_temp",
                self._create_phone_contacts_temp,
                debug=debug,
            )
            email_contact_count = self._time_step(
                "create_email_contacts_temp",
                self._create_email_contacts_temp,
                debug=debug,
            )

        self._time_step(
            "scan_direct_sources",
            lambda: (self._scan_direct_sources(params, debug=debug) or 0),
            debug=debug,
        )
        self._time_step(
            "scan_communication_sources",
            lambda: (
                self._scan_communication_sources(
                    params,
                    debug=debug,
                    include_contact_scans=include_contact_scans,
                )
                or 0
            ),
            debug=debug,
            include_contact_scans=include_contact_scans,
        )
        source_counts = self._time_step(
            "activity_source_counts",
            lambda: self._rows(
                """
                SELECT source, COUNT(DISTINCT booking_key) AS booking_count, SUM(event_count)::bigint AS event_count
                FROM ae_recent_booking_activity
                GROUP BY source
                ORDER BY source
                """
            ),
            debug=debug,
        )
        result = {
            "context_version": CONTEXT_VERSION_IDS,
            "mode": "ids_paged",
            "days": safe_days,
            "window": {
                "start": start_dt.isoformat(sep=" "),
                "end": end_dt.isoformat(sep=" "),
                "timezone": "Asia/Kolkata",
            },
            "active_booking_count": active_count,
            "phone_contact_count": phone_contact_count,
            "email_contact_count": email_contact_count,
            "source_counts": source_counts,
            "prepare_elapsed_ms": round((time.perf_counter() - total_started) * 1000, 2),
        }
        if debug:
            result["debug"] = {
                "steps": self._debug_steps,
                "notes": [
                    "Temp activity scan is prepared once.",
                    "final_id_page() can now page IDs without rebuilding temp tables.",
                ],
            }
        return _compact_dict(result)


    def final_id_page(self, *, limit: int = 100, offset: int = 0, debug: bool = False) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 100), 5000))
        safe_offset = max(0, int(offset or 0))
        return self._time_step(
            "final_distinct_booking_ids_page",
            lambda: self._final_ids(limit=safe_limit, offset=safe_offset),
            debug=debug,
            limit=safe_limit,
            offset=safe_offset,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def build_ids(
        self,
        *,
        days: int = 3,
        limit: int = 500,
        debug: bool = False,
        include_contact_scans: bool = False,
    ) -> Dict[str, Any]:
        self._debug_steps = []
        safe_days = max(1, min(int(days or 3), 365))
        safe_limit = max(1, min(int(limit or 500), 5000))
        end_dt = _now_local()
        start_dt = end_dt - timedelta(days=safe_days)
        today = end_dt.date()
        params = {
            "start_dt": start_dt,
            "end_dt": end_dt,
            "email_start_dt": start_dt,
            "email_end_dt": end_dt,
        }

        total_started = time.perf_counter()
        active_count = self._time_step(
            "create_active_bookings_temp",
            lambda: self._create_active_bookings_temp(today=today),
            debug=debug,
        )
        phone_contact_count = 0
        email_contact_count = 0
        if include_contact_scans:
            phone_contact_count = self._time_step(
                "create_phone_contacts_temp",
                self._create_phone_contacts_temp,
                debug=debug,
            )
            email_contact_count = self._time_step(
                "create_email_contacts_temp",
                self._create_email_contacts_temp,
                debug=debug,
            )
        self._time_step("scan_direct_sources", lambda: (self._scan_direct_sources(params, debug=debug) or 0), debug=debug)
        self._time_step(
            "scan_communication_sources",
            lambda: (self._scan_communication_sources(params, debug=debug, include_contact_scans=include_contact_scans) or 0),
            debug=debug,
            include_contact_scans=include_contact_scans,
        )
        source_counts = self._time_step(
            "activity_source_counts",
            lambda: self._rows(
                """
                SELECT source, COUNT(DISTINCT booking_key) AS booking_count, SUM(event_count)::bigint AS event_count
                FROM ae_recent_booking_activity
                GROUP BY source
                ORDER BY source
                """
            ),
            debug=debug,
        )
        rows = self._time_step("final_distinct_booking_ids", lambda: self._final_ids(limit=safe_limit), debug=debug)

        booking_ids = [int(row["booking_id"]) for row in rows if row.get("booking_id") is not None]
        total_ms = round((time.perf_counter() - total_started) * 1000, 2)
        slowest = sorted(self._debug_steps, key=lambda row: float(row.get("elapsed_ms") or 0), reverse=True)[:5]
        result = {
            "context_version": CONTEXT_VERSION_IDS,
            "mode": "ids_only",
            "days": safe_days,
            "window": {
                "start": start_dt.isoformat(sep=" "),
                "end": end_dt.isoformat(sep=" "),
                "timezone": "Asia/Kolkata",
            },
            "active_booking_count": active_count,
            "phone_contact_count": phone_contact_count,
            "email_contact_count": email_contact_count,
            "booking_count": len(booking_ids),
            "booking_ids": booking_ids,
            "source_counts": source_counts,
            "truncated": len(booking_ids) >= safe_limit,
        }
        if debug:
            result["debug"] = {
                "total_elapsed_ms": total_ms,
                "slowest_steps": slowest,
                "steps": self._debug_steps,
                "notes": [
                    "booking_confirm is intentionally excluded from activity sources; it only defines active booking scope.",
                    "include_contact_scans=false skips phone/email temp tables and call/WhatsApp/email scans for faster direct-activity tracking.",
                    "If create_active_bookings_temp is slow, add indexes on staging_booking_confirm booking_status/travel dates/prop_id and staging_property_unit prop_id/rms_prop.",
                ],
            }
        return _compact_dict(result)

    def build(
        self,
        *,
        days: int = 3,
        current_only: bool = True,  # kept for route compatibility; ignored by active-stay SQL
        limit: int = 500,
        event_limit: int = 10000,  # kept for compatibility
        max_events_per_booking: int = 5,  # kept for compatibility
        max_text: int = 180,  # kept for compatibility
        include_timeline: bool = False,
        ids_only: bool = True,
        debug: bool = False,
        include_contact_scans: bool = False,
    ) -> Dict[str, Any]:
        # The endpoint's hot path is ID-only. Full event/timeline output was the
        # slow part and is intentionally not expanded here unless reintroduced
        # behind a separate endpoint.
        return self.build_ids(
            days=days,
            limit=limit,
            debug=debug,
            include_contact_scans=include_contact_scans,
        )


__all__ = ["ActiveBookingActivityService"]
