# -----------------------------------------------------------------------------
# Repo bootstrap
# -----------------------------------------------------------------------------
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text


def _analytics_engine_project_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent, *current.parents]:
        if (parent / "app").is_dir():
            return parent
    raise RuntimeError("Could not find FastApi project root containing the app/ folder.")


PROJECT_ROOT = _analytics_engine_project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from app.db.database import get_db  # noqa: E402

try:  # noqa: E402
    from app.services.analytics_engine.core.config import STAGING_SOURCE_KIND_BY_TABLE
except Exception:  # pragma: no cover - lets this diagnostic script run during partial imports
    STAGING_SOURCE_KIND_BY_TABLE = {}

from app.services.analytics_engine.ingestion.staging_sync_service import AnalyticsStagingSyncService  # noqa: E402
from app.services.analytics_engine.ingestion.source_db import (  # noqa: E402
    get_source_engine,
    get_source_url_debug,
    get_thirdparty_mysql_engine,
    get_thirdparty_pg_engine,
    test_engine_connection,
)


# Keep this aligned with run_daily_stage_sync_all.DAILY_SYNC_TABLES and
# AnalyticsStagingSyncService.run_sync().  New master tables are staging-only;
# they do not get added to run_from_checkpoint_10k unless a processor exists.
SYNC_CONFIG_BY_TABLE: dict[str, dict[str, Any]] = {
    # CRM / identity / lead
    "user_account": {
        "method": "sync_user_accounts",
        "default_mode": "id",
        "default_limit": 50000,
        "source_kind": "thirdparty_mysql",
        "source_table": "a3m_account",
    },
    "admin_user_account": {
        "method": "sync_admin_user_accounts",
        "default_mode": "daily",
        "default_limit": 1000,
        "source_kind": "thirdparty_mysql",
        "source_table": "a3m_account",
    },
    "mobile_tagging": {
        "method": "sync_mobile_tagging",
        "default_mode": "daily",
        "default_limit": 5000,
        "source_kind": "thirdparty_mysql",
        "source_table": "MobileTagging",
    },
    "staff_phone_assignment": {
        "method": "sync_mobile_tagging",
        "default_mode": "daily",
        "default_limit": 5000,
        "source_kind": "thirdparty_mysql",
        "source_table": "MobileTagging",
        "alias_for": "mobile_tagging",
    },
    "building_details": {
        "method": "sync_buildings",
        "default_mode": "hybrid",
        "default_limit": 50000,
        "source_kind": "thirdparty_mysql",
        "source_table": "building_details",
    },
    "property_unit": {
        "method": "sync_property_units",
        "default_mode": "daily",
        "default_limit": 100000,
        "source_kind": "thirdparty_mysql",
        "source_table": "prop_tracking",
    },
    "lead_tracking": {
        "method": "sync_leads",
        "default_mode": "id",
        "default_limit": 50000,
        "source_kind": "thirdparty_mysql",
        "source_table": "lead_tracking",
    },

    # communication
    "whatsapp_messages": {
        "method": "sync_whatsapp_messages",
        "default_mode": "time",
        "default_limit": 50000,
        "source_kind": "thirdparty_pg",
        "source_table": "public.messages",
    },
    "email_messages": {
        "method": "sync_email_messages",
        "default_mode": "time",
        "default_limit": 30000,
        "source_kind": "thirdparty_pg",
        "source_table": "public.emails",
    },
    "call_recordings_transcript": {
        "method": "sync_call_recordings_transcript",
        "default_mode": "id",
        "default_limit": 50000,
        "source_kind": "thirdparty_pg",
        "source_table": "public.call_recordings_transcript",
    },
    # Legacy single-source alias.  It now points to the recordings transcript
    # staging sync; use call_log_unified for the new merged RMS+recording table.
    "call_log_tracking": {
        "method": "sync_call_recordings_transcript",
        "default_mode": "id",
        "default_limit": 50000,
        "source_kind": "thirdparty_pg",
        "source_table": "public.call_recordings_transcript",
        "alias_for": "call_recordings_transcript",
    },
    "call_log_unified": {
        "method": "__build_unified_call_log",
        "default_mode": "id",
        "default_limit": 50000,
        "source_kind": "mixed",
        "source_table": "call_tracking_log + public.call_recordings_transcript",
    },
    "unified_call_log": {
        "method": "__build_unified_call_log",
        "default_mode": "id",
        "default_limit": 50000,
        "source_kind": "mixed",
        "source_table": "call_tracking_log + public.call_recordings_transcript",
        "alias_for": "call_log_unified",
    },
    "staging_call_log_unified": {
        "method": "__build_unified_call_log",
        "default_mode": "id",
        "default_limit": 50000,
        "source_kind": "mixed",
        "source_table": "call_tracking_log + public.call_recordings_transcript",
        "alias_for": "call_log_unified",
    },

    # lifecycle / booking
    "booking_confirm": {
        "method": "sync_booking_confirm",
        "default_mode": "id",
        "default_limit": 50000,
        "source_kind": "thirdparty_mysql",
        "source_table": "booking_confirm",
    },
    "booking_audit_history": {
        "method": "sync_booking_audit_history",
        "default_mode": "id",
        "default_limit": 50000,
        "source_kind": "thirdparty_mysql",
        "source_table": "booking_audit_history",
    },
    "booking_invoice_details": {
        "method": "sync_booking_invoice_details",
        "default_mode": "id",
        "default_limit": 50000,
        "source_kind": "thirdparty_mysql",
        "source_table": "booking_invoice_details",
    },

    # customer activity
    "site_visits": {
        "method": "sync_site_visits",
        "default_mode": "id",
        "default_limit": 50000,
        "source_kind": "thirdparty_mysql",
        "source_table": "site_visits",
    },
    "travel_cart": {
        "method": "sync_travel_cart",
        "default_mode": "id",
        "default_limit": 50000,
        "source_kind": "thirdparty_mysql",
        "source_table": "travel_cart",
    },
    "user_wishlist": {
        "method": "sync_wishlist",
        "default_mode": "time",
        "default_limit": 50000,
        "source_kind": "thirdparty_mysql",
        "source_table": "user_wishlist",
    },
    "web_visits": {
        "method": "sync_web_visits",
        "default_mode": "id",
        "default_limit": 50000,
        "source_kind": "thirdparty_mysql",
        "source_table": "last_visited",
    },

    # stay/support
    "checkin_form": {
        "method": "sync_checkin_form",
        "default_mode": "id",
        "default_limit": 30000,
        "source_kind": "thirdparty_mysql",
        "source_table": "check_in_form",
    },
    "checkout_form": {
        "method": "sync_checkout_form",
        "default_mode": "id",
        "default_limit": 30000,
        "source_kind": "thirdparty_mysql",
        "source_table": "check_out_form",
    },
    "user_ticket": {
        "method": "sync_user_ticket",
        "default_mode": "id",
        "default_limit": 50000,
        "source_kind": "thirdparty_mysql",
        "source_table": "user_ticket",
    },

    # Kept for manual backfills/debugging even though it is not in the daily list.
    "user_contact_info": {
        "method": "sync_user_contact_info",
        "default_mode": "id",
        "default_limit": 20000,
        "source_kind": "thirdparty_mysql",
        "source_table": "user_contact_info",
    },
}

SYNC_METHOD_BY_TABLE = {table: cfg["method"] for table, cfg in SYNC_CONFIG_BY_TABLE.items()}
SOURCE_TABLE_BY_SYNC_TABLE = {table: cfg.get("source_table") for table, cfg in SYNC_CONFIG_BY_TABLE.items()}
UNIFIED_CALL_LOG_TABLES = {
    table
    for table, cfg in SYNC_CONFIG_BY_TABLE.items()
    if cfg.get("method") == "__build_unified_call_log"
}


SOURCE_KIND_ALIASES = {
    "thirdparty_mysql": "thirdparty_mysql",
    "mysql": "thirdparty_mysql",
    "third_party_mysql": "thirdparty_mysql",
    "rms_mysql": "thirdparty_mysql",
    "thirdparty_pg": "thirdparty_pg",
    "thirdparty_postgres": "thirdparty_pg",
    "thirdparty_postgresql": "thirdparty_pg",
    "third_party_pg": "thirdparty_pg",
    "postgres": "thirdparty_pg",
    "postgresql": "thirdparty_pg",
    "pg": "thirdparty_pg",
    "mixed": "mixed",
}


def _normalize_source_kind(source_kind: Any) -> str | None:
    text_value = str(source_kind or "").strip().lower()
    if not text_value:
        return None
    return SOURCE_KIND_ALIASES.get(text_value, text_value)


def _source_url_debug(source_kind: str | None) -> dict[str, Any]:
    """Return best-effort source URL diagnostics.

    Some repo versions expose get_thirdparty_mysql_engine()/get_thirdparty_pg_engine()
    but do not support those names inside get_source_url_debug(). Debug preflight
    should not fail before it tries the real engine helper.
    """
    normalized = _normalize_source_kind(source_kind)
    if not normalized:
        return {"ok": False, "error": "missing source_kind"}

    try:
        info = get_source_url_debug(normalized)
        if isinstance(info, dict):
            return info
        return {"ok": True, "source_kind": normalized, "value": info}
    except Exception as exc:
        if normalized in {"thirdparty_mysql", "thirdparty_pg"}:
            return {
                "ok": True,
                "source_kind": normalized,
                "note": (
                    "source_db.get_source_url_debug() does not support this source_kind "
                    "in this repo version; using the dedicated third-party engine helper "
                    "for the connection test."
                ),
                "debug_helper_error": f"{exc.__class__.__name__}: {exc}",
            }
        return {"ok": False, "source_kind": normalized, "error": f"{exc.__class__.__name__}: {exc}"}


def _source_engine(source_kind: str | None):
    normalized = _normalize_source_kind(source_kind)
    if normalized == "thirdparty_mysql":
        return get_thirdparty_mysql_engine()
    if normalized == "thirdparty_pg":
        return get_thirdparty_pg_engine()
    if normalized is None:
        raise ValueError("missing source_kind")
    return get_source_engine(normalized)


def _test_source_connection(engine, source_kind: str | None) -> dict[str, Any]:
    """Connection test that uses SQL compatible with the actual source DB.

    Keep this intentionally defensive.  The debug preflight should validate the
    connection, but it should not fail just because a diagnostic expression or
    alias is unsupported by a specific MySQL/MariaDB/proxy version.
    """
    normalized = _normalize_source_kind(source_kind)
    dialect = str(getattr(getattr(engine, "dialect", None), "name", "") or "").lower()
    is_mysql = normalized == "thirdparty_mysql" or dialect.startswith("mysql")
    is_pg = normalized == "thirdparty_pg" or dialect in {"postgresql", "postgres"}

    if is_mysql:
        # Avoid aliasing as current_user because it is reserved/special in some
        # MySQL variants. USER() + @@version are widely supported.
        sql = """
        SELECT
            DATABASE() AS database_name,
            USER() AS user_name,
            @@version AS server_version
        """
    elif is_pg:
        sql = """
        SELECT
            current_database() AS database_name,
            current_schema() AS current_schema,
            inet_server_addr()::text AS server_addr,
            inet_server_port() AS server_port,
            current_user AS current_user_name
        """
    else:
        try:
            return test_engine_connection(engine)
        except Exception:
            sql = "SELECT 1 AS ok"

    try:
        with engine.connect() as conn:
            row = conn.execute(text(sql)).mappings().fetchone()
        return {
            "ok": True,
            "source_kind": normalized,
            "dialect": dialect,
            "details": dict(row) if row else {},
        }
    except Exception as exc:
        # If the DB accepted the connection but rejected only the diagnostic SQL,
        # fall back to SELECT 1.  This prevents --debug-source from blocking the
        # real sync because of a harmless metadata-query compatibility issue.
        if is_mysql:
            original_error = str(exc)
            try:
                with engine.connect() as conn:
                    row = conn.execute(text("SELECT 1 AS ok")).mappings().fetchone()
                return {
                    "ok": True,
                    "source_kind": normalized,
                    "dialect": dialect,
                    "details": dict(row) if row else {"ok": 1},
                    "note": "MySQL diagnostic query failed; basic SELECT 1 connection test passed.",
                    "diagnostic_error_type": exc.__class__.__name__,
                    "diagnostic_error": original_error,
                }
            except Exception as fallback_exc:
                return {
                    "ok": False,
                    "source_kind": normalized,
                    "dialect": dialect,
                    "error_type": fallback_exc.__class__.__name__,
                    "error": str(fallback_exc),
                    "diagnostic_error_type": exc.__class__.__name__,
                    "diagnostic_error": original_error,
                }

        return {
            "ok": False,
            "source_kind": normalized,
            "dialect": dialect,
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        }


def _print_jsonish(label: str, value: Any) -> None:
    print(f"{label}:")
    try:
        print(json.dumps(value, indent=2, default=str, ensure_ascii=False))
    except Exception:
        print(value)


def _source_kind_for_table(table: str) -> str | None:
    cfg = SYNC_CONFIG_BY_TABLE.get(table) or {}
    # Prefer this script's table config for aliases whose meaning changed.
    return _normalize_source_kind(cfg.get("source_kind") or STAGING_SOURCE_KIND_BY_TABLE.get(table))


def _source_table_for_table(table: str) -> str | None:
    cfg = SYNC_CONFIG_BY_TABLE.get(table) or {}
    return cfg.get("source_table")


def _source_table_exists_sql(source_table: str, source_kind: str) -> tuple[str, dict[str, Any]]:
    source_kind = _normalize_source_kind(source_kind) or source_kind
    if source_kind == "thirdparty_pg":
        if "." in source_table:
            schema_name, table_name = source_table.split(".", 1)
        else:
            schema_name, table_name = "public", source_table
        return (
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

    # MySQL source tables live in the connected database/schema.
    table_name = source_table.split(".", 1)[-1]
    return (
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_name = :table_name
        ) AS present
        """,
        {"table_name": table_name},
    )


def _source_stats_sql(table: str) -> str:
    # PostgreSQL / PeerDB sources.
    if table == "whatsapp_messages":
        return """
        SELECT
            COUNT(*) AS total_rows,
            COUNT(*) FILTER (WHERE timestamp IS NOT NULL) AS rows_with_cursor,
            MIN(timestamp) AS min_cursor,
            MAX(timestamp) AS max_cursor
        FROM public.messages
        """
    if table == "email_messages":
        return """
        SELECT
            COUNT(*) AS total_rows,
            COUNT(*) FILTER (WHERE date IS NOT NULL) AS rows_with_cursor,
            MIN(date) AS min_cursor,
            MAX(date) AS max_cursor,
            MIN(id) AS min_id,
            MAX(id) AS max_id
        FROM public.emails
        """
    if table in {"call_recordings_transcript", "call_log_tracking"}:
        return """
        SELECT
            COUNT(*) AS total_rows,
            COUNT(*) FILTER (WHERE COALESCE(call_datetime, uploaded_at) IS NOT NULL) AS rows_with_cursor,
            MIN(COALESCE(call_datetime, uploaded_at)) AS min_cursor,
            MAX(COALESCE(call_datetime, uploaded_at)) AS max_cursor,
            MIN(id) AS min_id,
            MAX(id) AS max_id
        FROM public.call_recordings_transcript
        WHERE COALESCE("_PEERDB_IS_DELETED", false) = false
        """

    # MySQL source tables.  Avoid PostgreSQL-only FILTER syntax here.
    if table in {"user_account", "admin_user_account"}:
        where = "WHERE a.team IS NOT NULL AND TRIM(CAST(a.team AS CHAR)) <> '' AND TRIM(CAST(a.team AS CHAR)) <> '0'" if table == "admin_user_account" else ""
        return f"""
        SELECT
            COUNT(*) AS total_rows,
            SUM(CASE WHEN a.createdon IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cursor,
            MIN(a.createdon) AS min_cursor,
            MAX(a.createdon) AS max_cursor,
            MIN(a.id) AS min_id,
            MAX(a.id) AS max_id
        FROM a3m_account a
        {where}
        """
    if table in {"mobile_tagging", "staff_phone_assignment"}:
        return """
        SELECT
            COUNT(*) AS total_rows,
            SUM(CASE WHEN last_update_time IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cursor,
            MIN(last_update_time) AS min_cursor,
            MAX(last_update_time) AS max_cursor
        FROM MobileTagging
        WHERE Phone IS NOT NULL
          AND TRIM(CAST(Phone AS CHAR)) <> ''
        """
    if table == "building_details":
        return """
        SELECT
            COUNT(*) AS total_rows,
            SUM(CASE WHEN updated_on IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cursor,
            MIN(updated_on) AS min_cursor,
            MAX(updated_on) AS max_cursor,
            MIN(bid) AS min_id,
            MAX(bid) AS max_id
        FROM building_details
        WHERE status <> 1
        """
    if table == "property_unit":
        return """
        SELECT
            COUNT(*) AS total_rows,
            SUM(CASE WHEN COALESCE(pt.last_updated_on, pi.last_update_time, pt.added_on) IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cursor,
            MIN(COALESCE(pt.last_updated_on, pi.last_update_time, pt.added_on)) AS min_cursor,
            MAX(COALESCE(pt.last_updated_on, pi.last_update_time, pt.added_on)) AS max_cursor,
            MIN(pt.id) AS min_id,
            MAX(pt.id) AS max_id
        FROM prop_tracking pt
        LEFT JOIN prop_info pi ON pi.prop_id = pt.prop_id
        WHERE pt.rms_prop = 'RMS Prop'
        """
    if table == "lead_tracking":
        return """
        SELECT
            COUNT(*) AS total_rows,
            SUM(CASE WHEN added_on IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cursor,
            MIN(added_on) AS min_cursor,
            MAX(added_on) AS max_cursor,
            MIN(id) AS min_id,
            MAX(id) AS max_id
        FROM lead_tracking
        """
    if table == "site_visits":
        return """
        SELECT
            COUNT(*) AS total_rows,
            SUM(CASE WHEN COALESCE(site_visit_date, added_on) IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cursor,
            MIN(COALESCE(site_visit_date, added_on)) AS min_cursor,
            MAX(COALESCE(site_visit_date, added_on)) AS max_cursor,
            MIN(id) AS min_id,
            MAX(id) AS max_id
        FROM site_visits
        """
    if table == "travel_cart":
        return """
        SELECT
            COUNT(*) AS total_rows,
            SUM(CASE WHEN added_on IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cursor,
            MIN(added_on) AS min_cursor,
            MAX(added_on) AS max_cursor,
            MIN(travel_id) AS min_id,
            MAX(travel_id) AS max_id
        FROM travel_cart
        """
    if table == "user_wishlist":
        return """
        SELECT
            COUNT(*) AS total_rows,
            SUM(CASE WHEN added_on IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cursor,
            MIN(added_on) AS min_cursor,
            MAX(added_on) AS max_cursor
        FROM user_wishlist
        """
    if table == "web_visits":
        return """
        SELECT COUNT(*) AS total_rows, MIN(id) AS min_id, MAX(id) AS max_id
        FROM last_visited
        """
    if table == "checkin_form":
        return """
        SELECT
            COUNT(*) AS total_rows,
            SUM(CASE WHEN COALESCE(check_in_date, added_on) IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cursor,
            MIN(COALESCE(check_in_date, added_on)) AS min_cursor,
            MAX(COALESCE(check_in_date, added_on)) AS max_cursor,
            MIN(id) AS min_id,
            MAX(id) AS max_id
        FROM check_in_form
        """
    if table == "checkout_form":
        return """
        SELECT
            COUNT(*) AS total_rows,
            SUM(CASE WHEN COALESCE(chk_out_date, added_time) IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cursor,
            MIN(COALESCE(chk_out_date, added_time)) AS min_cursor,
            MAX(COALESCE(chk_out_date, added_time)) AS max_cursor,
            MIN(id) AS min_id,
            MAX(id) AS max_id
        FROM check_out_form
        """
    if table == "user_ticket":
        return """
        SELECT
            COUNT(*) AS total_rows,
            SUM(CASE WHEN date IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cursor,
            MIN(date) AS min_cursor,
            MAX(date) AS max_cursor,
            MIN(id) AS min_id,
            MAX(id) AS max_id
        FROM user_ticket
        """
    if table == "booking_confirm":
        return """
        SELECT
            COUNT(*) AS total_rows,
            SUM(CASE WHEN booking_datetime IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cursor,
            MIN(booking_datetime) AS min_cursor,
            MAX(booking_datetime) AS max_cursor,
            MIN(booking_id) AS min_id,
            MAX(booking_id) AS max_id
        FROM booking_confirm
        """
    if table == "booking_audit_history":
        return """
        SELECT
            COUNT(*) AS total_rows,
            SUM(CASE WHEN added_time IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cursor,
            MIN(added_time) AS min_cursor,
            MAX(added_time) AS max_cursor,
            MIN(id) AS min_id,
            MAX(id) AS max_id
        FROM booking_audit_history
        """
    if table == "booking_invoice_details":
        return """
        SELECT
            COUNT(*) AS total_rows,
            SUM(CASE WHEN COALESCE(utr_added_on, send_time, Created_on) IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cursor,
            MIN(COALESCE(utr_added_on, send_time, Created_on)) AS min_cursor,
            MAX(COALESCE(utr_added_on, send_time, Created_on)) AS max_cursor,
            MIN(invoice_id) AS min_id,
            MAX(invoice_id) AS max_id
        FROM booking_invoice_details
        """
    if table == "user_contact_info":
        return """
        SELECT
            COUNT(*) AS total_rows,
            SUM(CASE WHEN added_on IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cursor,
            MIN(added_on) AS min_cursor,
            MAX(added_on) AS max_cursor,
            MIN(id) AS min_id,
            MAX(id) AS max_id
        FROM user_contact_info
        """
    return ""


def _latest_sample_sql(table: str) -> str:
    if table == "whatsapp_messages":
        return """
        SELECT
            message_id,
            admin_number,
            cx_number,
            direction,
            timestamp,
            message_type,
            remote_jid,
            LEFT(COALESCE(clean_content, ''), 120) AS preview
        FROM public.messages
        ORDER BY timestamp DESC NULLS LAST, message_id DESC
        LIMIT 5
        """
    if table == "email_messages":
        return """
        SELECT id, date, direction, subject, LEFT(COALESCE(snippet, body, ''), 120) AS preview
        FROM public.emails
        ORDER BY date DESC NULLS LAST, id DESC
        LIMIT 5
        """
    if table in {"call_recordings_transcript", "call_log_tracking"}:
        return """
        SELECT id, call_datetime, uploaded_at, emp_name, department, call_type, LEFT(COALESCE(transcript_text, translated_text, raw_transcripts, ''), 120) AS preview
        FROM public.call_recordings_transcript
        WHERE COALESCE("_PEERDB_IS_DELETED", false) = false
        ORDER BY COALESCE(call_datetime, uploaded_at) DESC NULLS LAST, id DESC
        LIMIT 5
        """
    return ""


def source_preflight(table: str, *, require_source_rows: bool = False, verbose: bool = False) -> dict[str, Any]:
    if table in UNIFIED_CALL_LOG_TABLES:
        return unified_call_log_preflight(require_source_rows=require_source_rows, verbose=verbose)

    source_kind = _source_kind_for_table(table)
    source_table = _source_table_for_table(table)

    result: dict[str, Any] = {
        "table": table,
        "source_kind": source_kind,
        "source_table": source_table,
        "ok": False,
    }

    if not source_kind:
        result["error"] = f"No source kind configured for table={table!r}"
        return result

    url_info = _source_url_debug(source_kind)
    result["url"] = url_info

    if not url_info.get("ok"):
        result["error"] = (
            f"Source DB URL is not configured for {source_kind}. "
            f"Details: {url_info.get('error')}"
        )
        return result

    try:
        engine = _source_engine(source_kind)
    except Exception as exc:
        result["error"] = f"Could not create source engine for {source_kind}: {exc}"
        result["error_type"] = exc.__class__.__name__
        return result

    conn_info = _test_source_connection(engine, source_kind)
    result["connection"] = conn_info

    if not conn_info.get("ok"):
        result["error"] = (
            f"Source DB connection failed for {source_kind}: "
            f"{conn_info.get('error_type')}: {conn_info.get('error')}"
        )
        return result

    if not source_table:
        result["ok"] = True
        return result

    try:
        with engine.begin() as conn:
            exists_sql, exists_params = _source_table_exists_sql(source_table, source_kind)
            exists_row = conn.execute(text(exists_sql), exists_params).mappings().fetchone()
            exists = bool(exists_row and exists_row.get("present"))
            result["source_table_exists"] = exists

            if not exists:
                result["error"] = f"Source table {source_table} does not exist in connected {source_kind} DB."
                return result

            stats_sql = _source_stats_sql(table)
            if stats_sql:
                stats = conn.execute(text(stats_sql)).mappings().fetchone()
                result["source_stats"] = dict(stats) if stats else {}
                if require_source_rows and int((result["source_stats"] or {}).get("total_rows") or 0) <= 0:
                    result["error"] = (
                        f"Source table {source_table} exists but has 0 rows. "
                        "This usually means the URL points to the wrong/stale source DB."
                    )
                    return result

            sample_sql = _latest_sample_sql(table)
            if verbose and sample_sql:
                sample = conn.execute(text(sample_sql)).mappings().fetchall()
                result["latest_sample"] = [dict(r) for r in sample]

    except Exception as exc:
        result["error"] = f"Source preflight query failed: {exc}"
        result["error_type"] = exc.__class__.__name__
        return result

    result["ok"] = True
    return result


def unified_call_log_preflight(*, require_source_rows: bool = False, verbose: bool = False) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "table": "call_log_unified",
        "source_kind": "mixed",
        "source_table": "call_tracking_log + public.call_recordings_transcript",
        "ok": False,
        "sources": {},
    }

    source_specs = {
        "rms_mysql": {
            "source_kind": "thirdparty_mysql",
            "source_table": "call_tracking_log",
            "stats_sql": """
                SELECT COUNT(*) AS total_rows, MIN(id) AS min_id, MAX(id) AS max_id,
                       MIN(callDate) AS min_cursor, MAX(callDate) AS max_cursor
                FROM call_tracking_log
            """,
        },
        "recordings_pg": {
            "source_kind": "thirdparty_pg",
            "source_table": "public.call_recordings_transcript",
            "stats_sql": """
                SELECT COUNT(*) AS total_rows, MIN(id) AS min_id, MAX(id) AS max_id,
                       MIN(COALESCE(call_datetime, uploaded_at)) AS min_cursor,
                       MAX(COALESCE(call_datetime, uploaded_at)) AS max_cursor
                FROM public.call_recordings_transcript
                WHERE COALESCE("_PEERDB_IS_DELETED", false) = false
            """,
        },
    }

    ok = True
    for label, spec in source_specs.items():
        source_kind = spec["source_kind"]
        source_table = spec["source_table"]
        item: dict[str, Any] = {
            "source_kind": source_kind,
            "source_table": source_table,
            "url": _source_url_debug(source_kind),
        }
        if not item["url"].get("ok"):
            item["error"] = f"Source DB URL is not configured for {source_kind}: {item['url'].get('error')}"
            checks["sources"][label] = item
            ok = False
            continue
        try:
            engine = _source_engine(source_kind)
            item["connection"] = _test_source_connection(engine, source_kind)
            if not item["connection"].get("ok"):
                item["error"] = f"Source DB connection failed: {item['connection'].get('error')}"
                checks["sources"][label] = item
                ok = False
                continue
            with engine.begin() as conn:
                exists_sql, exists_params = _source_table_exists_sql(source_table, source_kind)
                exists_row = conn.execute(text(exists_sql), exists_params).mappings().fetchone()
                item["source_table_exists"] = bool(exists_row and exists_row.get("present"))
                if not item["source_table_exists"]:
                    item["error"] = f"Source table {source_table} does not exist."
                    ok = False
                else:
                    stats = conn.execute(text(spec["stats_sql"])).mappings().fetchone()
                    item["source_stats"] = dict(stats) if stats else {}
                    if require_source_rows and int((item["source_stats"] or {}).get("total_rows") or 0) <= 0:
                        item["error"] = f"Source table {source_table} exists but has 0 rows."
                        ok = False
        except Exception as exc:
            item["error"] = f"Source preflight query failed: {exc}"
            item["error_type"] = exc.__class__.__name__
            ok = False
        checks["sources"][label] = item

    checks["ok"] = ok
    return checks


def run_unified_call_log_build(
    db,
    *,
    limit: int,
    resync_window: int,
    match_window_seconds: int,
    skip_rms: bool,
    skip_recordings: bool,
    rebuild_all: bool,
    reset_unified: bool,
    reset_raw: bool,
    match_only: bool,
    skip_counts: bool,
    skip_duplicate_refresh: bool,
    skip_backfill_existing_keys: bool,
    skip_match: bool,
    diagnostics: bool,
    no_timing: bool,
) -> dict[str, Any]:
    from app.services.analytics_engine.jobs.regular.build_unified_call_log import (  # noqa: WPS433
        UnifiedCallLogBuilder,
    )

    builder = UnifiedCallLogBuilder(
        db,
        match_window_seconds=match_window_seconds,
        timing_enabled=not no_timing,
    )
    return builder.run(
        limit=int(limit),
        resync_window=int(resync_window),
        skip_rms=bool(skip_rms),
        skip_recordings=bool(skip_recordings),
        rebuild_all=bool(rebuild_all),
        reset_unified=bool(reset_unified),
        reset_raw=bool(reset_raw),
        diagnostics=bool(diagnostics),
        skip_counts=bool(skip_counts),
        refresh_duplicate_counts=not bool(skip_duplicate_refresh),
        backfill_existing_keys=not bool(skip_backfill_existing_keys),
        match_after_insert=not bool(skip_match),
        match_only=bool(match_only),
    )


def _effective_mode(table: str, mode: str | None) -> str:
    cfg = SYNC_CONFIG_BY_TABLE.get(table) or {}
    if str(mode or "auto").strip().lower() == "auto":
        return str(cfg.get("default_mode") or "id")
    return str(mode)


def _effective_limit(table: str, limit: int | None) -> int:
    cfg = SYNC_CONFIG_BY_TABLE.get(table) or {}
    return int(limit or cfg.get("default_limit") or 50000)


def run(
    table: str,
    mode: str = "auto",
    limit: int | None = None,
    debug_source: bool = False,
    require_source_rows: bool = False,
    *,
    resync_window: int = 5000,
    match_window_seconds: int = 300,
    skip_rms: bool = False,
    skip_recordings: bool = False,
    rebuild_all: bool = False,
    reset_unified: bool = False,
    reset_raw: bool = False,
    match_only: bool = False,
    skip_counts: bool = False,
    skip_duplicate_refresh: bool = False,
    skip_backfill_existing_keys: bool = False,
    skip_match: bool = False,
    diagnostics: bool = False,
    no_timing: bool = False,
):
    method_name = SYNC_METHOD_BY_TABLE.get(table)
    if not method_name:
        allowed = ", ".join(sorted(SYNC_METHOD_BY_TABLE))
        raise ValueError(f"Unknown sync table={table!r}. Allowed: {allowed}")

    effective_mode = _effective_mode(table, mode)
    effective_limit = _effective_limit(table, limit)

    db_gen = get_db()
    db = next(db_gen)

    try:
        print("=" * 80)
        print("AnalyticsEngine staging sync")
        print("=" * 80)
        print(f"table={table}")
        print(f"method={method_name}")
        print(f"mode={effective_mode}")
        print(f"limit={effective_limit}")

        source_kind = _source_kind_for_table(table)
        should_preflight = debug_source or source_kind in {"thirdparty_pg", "mixed"} or require_source_rows

        if should_preflight:
            print("\nSource preflight")
            print("-" * 80)
            preflight = source_preflight(
                table,
                require_source_rows=require_source_rows,
                verbose=debug_source,
            )
            _print_jsonish("preflight", preflight)

            if not preflight.get("ok"):
                print("\nFAILED BEFORE SYNC")
                print("-" * 80)
                print(preflight.get("error") or "Unknown source preflight failure.")
                raise SystemExit(2)

        start_time = time.perf_counter()

        if method_name == "__build_unified_call_log":
            result = run_unified_call_log_build(
                db,
                limit=effective_limit,
                resync_window=resync_window,
                match_window_seconds=match_window_seconds,
                skip_rms=skip_rms,
                skip_recordings=skip_recordings,
                rebuild_all=rebuild_all,
                reset_unified=reset_unified,
                reset_raw=reset_raw,
                match_only=match_only,
                skip_counts=skip_counts,
                skip_duplicate_refresh=skip_duplicate_refresh,
                skip_backfill_existing_keys=skip_backfill_existing_keys,
                skip_match=skip_match,
                diagnostics=diagnostics,
                no_timing=no_timing,
            )
        else:
            service = AnalyticsStagingSyncService(db)
            # Use the service registry so this script cannot drift from staging_sync_service.run_sync().
            result = service.run_sync(
                sync_name=table,
                limit=effective_limit,
                mode=effective_mode,
            )

        elapsed = time.perf_counter() - start_time

        print(f"\nTotal execution time: {elapsed:.2f} seconds")
        print("-" * 80)
        for key, value in (result or {}).items():
            print(f"{key}: {value}")

        inserted = int((result or {}).get("inserted_or_updated") or 0)
        if inserted == 0 and should_preflight and method_name != "__build_unified_call_log":
            print("\nNo rows were inserted/updated.")
            print("-" * 80)
            print("Source connection was successful, but the sync query returned no new rows.")
            print("Check checkpoint cursor versus source max cursor above, or daily-refresh skip status.")

        return result

    except SystemExit:
        raise
    except Exception as exc:
        print("\nSYNC FAILED")
        print("-" * 80)
        print(f"{exc.__class__.__name__}: {exc}")
        raise
    finally:
        try:
            db.close()
        except Exception:
            pass
        try:
            next(db_gen, None)
        except Exception:
            pass


def list_tables() -> None:
    print("Available sync tables:")
    for table in sorted(SYNC_CONFIG_BY_TABLE):
        cfg = SYNC_CONFIG_BY_TABLE[table]
        alias = f" alias_for={cfg['alias_for']}" if cfg.get("alias_for") else ""
        print(
            f"  {table:28s} mode={cfg.get('default_mode'):8s} "
            f"limit={cfg.get('default_limit'):8} method={cfg.get('method')}{alias}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--table", choices=sorted(SYNC_METHOD_BY_TABLE.keys()))
    parser.add_argument("--mode", choices=["auto", "id", "time", "daily", "full_daily", "hybrid"], default="auto")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--list-tables", action="store_true")
    parser.add_argument(
        "--debug-source",
        action="store_true",
        help="Print source DB URL/env resolution, DB identity, source table stats, and sample rows where supported.",
    )
    parser.add_argument(
        "--require-source-rows",
        action="store_true",
        help="Fail before sync if the source table exists but has 0 rows.",
    )

    # Options used only by --table call_log_unified / unified_call_log.
    parser.add_argument("--resync-window", type=int, default=5000)
    parser.add_argument("--match-window-seconds", type=int, default=300)
    parser.add_argument("--skip-rms", action="store_true")
    parser.add_argument("--skip-recordings", action="store_true")
    parser.add_argument("--rebuild-all", action="store_true")
    parser.add_argument("--reset-unified", action="store_true")
    parser.add_argument("--reset-raw", action="store_true")
    parser.add_argument("--match-only", action="store_true")
    parser.add_argument("--skip-counts", action="store_true")
    parser.add_argument("--skip-duplicate-refresh", action="store_true")
    parser.add_argument("--skip-backfill-existing-keys", action="store_true")
    parser.add_argument("--skip-match", action="store_true")
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--no-timing", action="store_true")
    args = parser.parse_args()

    if args.list_tables:
        list_tables()
        raise SystemExit(0)
    if not args.table:
        parser.error("--table is required unless --list-tables is used")

    run(
        table=args.table,
        mode=args.mode,
        limit=args.limit,
        debug_source=args.debug_source,
        require_source_rows=args.require_source_rows,
        resync_window=args.resync_window,
        match_window_seconds=args.match_window_seconds,
        skip_rms=args.skip_rms,
        skip_recordings=args.skip_recordings,
        rebuild_all=args.rebuild_all,
        reset_unified=args.reset_unified,
        reset_raw=args.reset_raw,
        match_only=args.match_only,
        skip_counts=args.skip_counts,
        skip_duplicate_refresh=args.skip_duplicate_refresh,
        skip_backfill_existing_keys=args.skip_backfill_existing_keys,
        skip_match=args.skip_match,
        diagnostics=args.diagnostics,
        no_timing=args.no_timing,
    )
