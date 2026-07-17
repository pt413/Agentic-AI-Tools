from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

DEFAULT_SCHEMA = os.getenv("SCHEMA_NAME", "AnalyticsEngine")
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
BOOKING_REVIEW_CONTEXT_VERSION = "booking_communication_review:v1_booking_dashboard"
BOOKING_REVIEW_SOURCE_SCOPE = "booking_id_only"
BOOKING_LLM_REVIEW_CACHE_TABLE = "booking_communication_review"
LEGACY_BOOKING_FOLLOWUP_CACHE_TABLE = "booking_followup_llm_review_cache"
IST_OFFSET = timedelta(hours=5, minutes=30)
SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(value: str) -> str:
    text_value = str(value or "").strip()
    if not SAFE_IDENT_RE.fullmatch(text_value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return text_value


def schema_ident(schema: str) -> str:
    return f'"{_safe_ident(schema)}"'


def table_ref(schema: str, table_name: str) -> str:
    return f"{schema_ident(schema)}.{_safe_ident(table_name)}"


def q(db: Session, sql: str, params: Optional[dict] = None) -> List[Dict[str, Any]]:
    return [dict(row._mapping) for row in db.execute(text(sql), params or {}).fetchall()]


def q1(db: Session, sql: str, params: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    rows = q(db, sql, params)
    return rows[0] if rows else None


def table_exists(db: Session, schema: str, table_name: str) -> bool:
    row = q1(
        db,
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = :schema_name
              AND table_name = :table_name
        ) AS present
        """,
        {"schema_name": schema, "table_name": table_name},
    )
    return bool(row and row.get("present"))


def table_columns(db: Session, schema: str, table_name: str) -> set[str]:
    if not table_exists(db, schema, table_name):
        return set()
    rows = q(
        db,
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = :schema_name
          AND table_name = :table_name
        """,
        {"schema_name": schema, "table_name": table_name},
    )
    return {str(row.get("column_name")) for row in rows if row.get("column_name")}


def compact_dict(value: Optional[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in (value or {}).items():
        if item in (None, "", [], {}, ()):  # keep 0 and False
            continue
        if isinstance(item, dict):
            child = compact_dict(item)
            if child:
                out[key] = child
        elif isinstance(item, list):
            cleaned = []
            for row in item:
                if isinstance(row, dict):
                    child = compact_dict(row)
                    if child:
                        cleaned.append(child)
                elif row not in (None, "", [], {}, ()):  # keep 0 and False
                    cleaned.append(row)
            if cleaned:
                out[key] = cleaned
        else:
            out[key] = item
    return out


def json_dumps(value: Any, *, pretty: bool = False) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2 if pretty else None, default=str, sort_keys=not pretty)


def json_param(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def parse_jsonish(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text_value = value.strip()
        if not text_value:
            return default
        try:
            return json.loads(text_value)
        except Exception:
            return default
    return default


def now_ist_naive() -> datetime:
    return datetime.utcnow() + IST_OFFSET


def today_ist() -> date:
    return now_ist_naive().date()


def coerce_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text_value = str(value).strip().lower()
    if text_value in {"1", "true", "yes", "y", "on"}:
        return True
    if text_value in {"0", "false", "no", "n", "off"}:
        return False
    return default


def parse_booking_ids_csv(value: Any) -> list[int]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        raw_items: list[Any] = []
        for item in value:
            if isinstance(item, str) and "," in item:
                raw_items.extend(item.split(","))
            else:
                raw_items.append(item)
    else:
        raw_items = str(value).replace("\n", ",").split(",")

    out: list[int] = []
    seen: set[int] = set()
    for item in raw_items:
        text_value = str(item or "").strip()
        if not text_value:
            continue
        if not re.fullmatch(r"\d+", text_value):
            raise ValueError(f"Invalid booking id: {text_value!r}")
        booking_id = int(text_value)
        if booking_id not in seen:
            seen.add(booking_id)
            out.append(booking_id)
    return out


def score_number(value: Any) -> Optional[int | float]:
    match = re.search(r"\b(10|[0-9](?:\.\d+)?)\s*/\s*10\b|\b(10|[0-9](?:\.\d+)?)\b", str(value or ""), flags=re.I)
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    try:
        score = float(raw)
        return int(score) if score.is_integer() else score
    except Exception:
        return None


def build_in_params(values: Sequence[Any], prefix: str) -> Tuple[str, Dict[str, Any]]:
    params: Dict[str, Any] = {}
    holders: List[str] = []
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
