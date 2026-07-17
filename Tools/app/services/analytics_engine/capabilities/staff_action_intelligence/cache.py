from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.analytics_engine.capabilities.booking_management.common import (
    DEFAULT_SCHEMA,
    compact_dict,
    json_param,
    parse_jsonish,
    q,
    q1,
    schema_ident,
    table_ref,
)


STAFF_ACTION_INTELLIGENCE_CACHE_TABLE = "staff_action_intelligence_review"
STAFF_ACTION_INTELLIGENCE_CONTEXT_VERSION = "staff_action_intelligence:v1"
ACTION_STATUS_VALUES = {"open", "assigned", "resolved", "verified", "ignored"}
_STAFF_ACTION_INTELLIGENCE_ENSURED: set[str] = set()

JSONB_COLUMNS = {
    "business_impact_json",
    "key_findings_json",
    "recommended_actions_json",
    "coaching_points_json",
    "data_gaps_json",
    "evidence_counts_json",
    "llm_output_json",
    "source_context_json",
}
NO_UPDATE_COLUMNS = {"id", "created_at", "updated_at"}


STAFF_ACTION_INTELLIGENCE_COLUMNS: dict[str, str] = {
    "staff_key": "TEXT NOT NULL",
    "username": "TEXT",
    "email": "TEXT",
    "phone": "TEXT",
    "team": "TEXT",
    "role_scope": "TEXT",
    "window_days": "INT NOT NULL DEFAULT 7",
    "window_start": "TIMESTAMP",
    "window_end": "TIMESTAMP",
    "overall_score": "NUMERIC",
    "priority_score": "NUMERIC",
    "risk": "TEXT",
    "reason": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'open'",
    "business_impact_json": "JSONB NOT NULL DEFAULT '{}'::jsonb",
    "key_findings_json": "JSONB NOT NULL DEFAULT '[]'::jsonb",
    "recommended_actions_json": "JSONB NOT NULL DEFAULT '[]'::jsonb",
    "coaching_points_json": "JSONB NOT NULL DEFAULT '[]'::jsonb",
    "data_gaps_json": "JSONB NOT NULL DEFAULT '[]'::jsonb",
    "evidence_counts_json": "JSONB NOT NULL DEFAULT '{}'::jsonb",
    "llm_output_json": "JSONB NOT NULL DEFAULT '{}'::jsonb",
    "source_context_json": "JSONB NOT NULL DEFAULT '{}'::jsonb",
    "model": "TEXT",
    "context_version": "TEXT",
    "context_hash": "TEXT",
    "error": "TEXT",
    "rated_at": "TIMESTAMP",
    "created_at": "TIMESTAMP NOT NULL DEFAULT NOW()",
    "updated_at": "TIMESTAMP NOT NULL DEFAULT NOW()",
}


def ensure_staff_action_intelligence_cache_table(db: Session, schema: str = DEFAULT_SCHEMA, *, force: bool = False) -> None:
    ensure_key = str(schema or DEFAULT_SCHEMA)
    if not force and ensure_key in _STAFF_ACTION_INTELLIGENCE_ENSURED:
        return

    table = table_ref(schema, STAFF_ACTION_INTELLIGENCE_CACHE_TABLE)
    db.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_ident(schema)}"))
    db.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id BIGSERIAL PRIMARY KEY,
                staff_key TEXT NOT NULL,
                username TEXT,
                email TEXT,
                phone TEXT,
                team TEXT,
                role_scope TEXT,
                window_days INT NOT NULL DEFAULT 7,
                window_start TIMESTAMP,
                window_end TIMESTAMP,
                overall_score NUMERIC,
                priority_score NUMERIC,
                risk TEXT,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                business_impact_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                key_findings_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                recommended_actions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                coaching_points_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                data_gaps_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                evidence_counts_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                llm_output_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                source_context_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                model TEXT,
                context_version TEXT,
                context_hash TEXT,
                error TEXT,
                rated_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                CONSTRAINT ux_staff_action_intelligence_staff_window UNIQUE (staff_key, role_scope, window_days)
            )
            """
        )
    )

    for column_name, column_type in STAFF_ACTION_INTELLIGENCE_COLUMNS.items():
        db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column_name} {column_type}"))

    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_staff_action_intelligence_staff_window ON {table} (staff_key, window_days)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_staff_action_intelligence_team ON {table} (team)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_staff_action_intelligence_risk ON {table} (risk)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_staff_action_intelligence_priority ON {table} (priority_score DESC NULLS LAST)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_staff_action_intelligence_status ON {table} (status)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_staff_action_intelligence_rated_at ON {table} (rated_at DESC NULLS LAST)"))
    db.commit()
    _STAFF_ACTION_INTELLIGENCE_ENSURED.add(ensure_key)


def _aggregate_review_status(actions: list[dict[str, Any]]) -> str:
    normalized = [str((row or {}).get("status") or "").strip().lower() for row in actions if isinstance(row, dict)]
    normalized = [value for value in normalized if value]
    if not normalized:
        return "open"
    if any(value == "open" for value in normalized):
        return "open"
    if any(value == "assigned" for value in normalized):
        return "assigned"
    if normalized and all(value == "verified" for value in normalized):
        return "verified"
    if all(value in {"resolved", "verified"} for value in normalized):
        return "resolved"
    if all(value == "ignored" for value in normalized):
        return "ignored"
    return normalized[0]


def apply_action_status_update(actions: list[dict[str, Any]], action_index: int, new_status: str) -> list[dict[str, Any]]:
    status_value = str(new_status or "").strip().lower()
    if status_value not in ACTION_STATUS_VALUES:
        raise ValueError(f"Unsupported action status: {new_status!r}")
    index_value = int(action_index)
    normalized_actions = [dict(row) for row in (actions or []) if isinstance(row, dict)]
    if index_value < 0 or index_value >= len(normalized_actions):
        raise ValueError(f"action_index {index_value} is out of range for {len(normalized_actions)} action(s).")
    normalized_actions[index_value]["status"] = status_value
    return normalized_actions


def _row_to_review(row: dict[str, Any]) -> dict[str, Any]:
    staff = compact_dict(
        {
            "staff_key": row.get("staff_key"),
            "username": row.get("username"),
            "email": row.get("email"),
            "phone": row.get("phone"),
            "team": row.get("team"),
            "role_scope": row.get("role_scope"),
        }
    )
    rating = compact_dict(
        {
            "overall_score": row.get("overall_score"),
            "priority_score": row.get("priority_score"),
            "risk": row.get("risk"),
            "reason": row.get("reason"),
        }
    )
    business_impact = parse_jsonish(row.get("business_impact_json"), {}) or {}
    key_findings = parse_jsonish(row.get("key_findings_json"), []) or []
    recommended_actions = parse_jsonish(row.get("recommended_actions_json"), []) or []
    coaching_points = parse_jsonish(row.get("coaching_points_json"), []) or []
    data_gaps = parse_jsonish(row.get("data_gaps_json"), []) or []
    evidence_counts = parse_jsonish(row.get("evidence_counts_json"), {}) or {}
    llm_output = parse_jsonish(row.get("llm_output_json"), {}) or {}
    source_context = parse_jsonish(row.get("source_context_json"), {}) or {}

    return compact_dict(
        {
            "id": row.get("id"),
            "staff": staff,
            "window": {
                "days": row.get("window_days"),
                "start": row.get("window_start"),
                "end": row.get("window_end"),
            },
            "rating": rating,
            "business_impact": business_impact,
            "key_findings": key_findings,
            "recommended_actions": recommended_actions,
            "coaching_points": coaching_points,
            "data_gaps": data_gaps,
            "evidence_counts": evidence_counts,
            "status": row.get("status") or _aggregate_review_status(recommended_actions),
            "rated_at": row.get("rated_at"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "llm_output": llm_output,
            "source_context": source_context,
            "error": row.get("error"),
            "model": row.get("model"),
            "context_version": row.get("context_version"),
            "context_hash": row.get("context_hash"),
        }
    )


def get_staff_action_review(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    staff_key: str,
    role_scope: Optional[str] = None,
    window_days: int = 7,
) -> Optional[dict[str, Any]]:
    ensure_staff_action_intelligence_cache_table(db, schema)
    table = table_ref(schema, STAFF_ACTION_INTELLIGENCE_CACHE_TABLE)
    row = db.execute(
        text(
            f"""
            SELECT *
            FROM {table}
            WHERE staff_key = :staff_key
              AND window_days = :window_days
              AND (:role_scope IS NULL OR COALESCE(role_scope, '') = :role_scope)
            ORDER BY rated_at DESC NULLS LAST, id DESC
            LIMIT 1
            """
        ),
        {
            "staff_key": str(staff_key or "").strip(),
            "window_days": int(window_days or 7),
            "role_scope": str(role_scope).strip() if role_scope not in (None, "") else None,
        },
    ).mappings().first()
    return _row_to_review(dict(row)) if row else None


def get_staff_action_review_by_id(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    review_id: int,
) -> Optional[dict[str, Any]]:
    ensure_staff_action_intelligence_cache_table(db, schema)
    table = table_ref(schema, STAFF_ACTION_INTELLIGENCE_CACHE_TABLE)
    row = db.execute(
        text(f"SELECT * FROM {table} WHERE id = :review_id LIMIT 1"),
        {"review_id": int(review_id)},
    ).mappings().first()
    return _row_to_review(dict(row)) if row else None


def list_staff_action_reviews(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    days: int = 7,
    team: Optional[str] = None,
    risk: Optional[str] = None,
    status: Optional[str] = None,
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
    min_priority: Optional[float] = None,
    max_priority: Optional[float] = None,
    search: Optional[str] = None,
) -> list[dict[str, Any]]:
    ensure_staff_action_intelligence_cache_table(db, schema)
    table = table_ref(schema, STAFF_ACTION_INTELLIGENCE_CACHE_TABLE)

    where_parts = ["window_days = :window_days"]
    params: dict[str, Any] = {"window_days": int(days or 7)}

    if team not in (None, "", "all"):
        where_parts.append("LOWER(COALESCE(team, '')) = :team")
        params["team"] = str(team).strip().lower()
    if risk not in (None, "", "all"):
        where_parts.append("LOWER(COALESCE(risk, '')) = :risk")
        params["risk"] = str(risk).strip().lower()
    if status not in (None, "", "all"):
        where_parts.append("LOWER(COALESCE(status, '')) = :status")
        params["status"] = str(status).strip().lower()
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
    if search not in (None, ""):
        where_parts.append(
            "("
            "COALESCE(username, '') ILIKE :search OR "
            "COALESCE(email, '') ILIKE :search OR "
            "COALESCE(phone, '') ILIKE :search OR "
            "COALESCE(team, '') ILIKE :search OR "
            "COALESCE(role_scope, '') ILIKE :search OR "
            "COALESCE(reason, '') ILIKE :search OR "
            "COALESCE(business_impact_json::text, '') ILIKE :search OR "
            "COALESCE(key_findings_json::text, '') ILIKE :search OR "
            "COALESCE(recommended_actions_json::text, '') ILIKE :search"
            ")"
        )
        params["search"] = f"%{str(search).strip()}%"

    rows = q(
        db,
        f"""
        SELECT *
        FROM {table}
        WHERE {' AND '.join(where_parts)}
        ORDER BY rated_at DESC NULLS LAST, id DESC
        """,
        params,
    )
    return [_row_to_review(row) for row in rows]


def store_staff_action_review(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    ensure_staff_action_intelligence_cache_table(db, schema)
    table = table_ref(schema, STAFF_ACTION_INTELLIGENCE_CACHE_TABLE)

    row = {column: payload.get(column) for column in STAFF_ACTION_INTELLIGENCE_COLUMNS}
    row["status"] = payload.get("status") or _aggregate_review_status(payload.get("recommended_actions_json") or [])
    columns = list(row.keys())
    params: dict[str, Any] = {}
    insert_values: list[str] = []

    for column in columns:
        value = row.get(column)
        if column in JSONB_COLUMNS:
            default_value = [] if column.endswith("_json") and column not in {"business_impact_json", "evidence_counts_json", "llm_output_json", "source_context_json"} else {}
            params[column] = json_param(value if value not in (None, "") else default_value)
            insert_values.append(f"CAST(:{column} AS jsonb)")
        elif column == "created_at" and value is None:
            insert_values.append("NOW()")
        elif column in {"updated_at", "rated_at"} and value is None:
            insert_values.append("NOW()")
        else:
            params[column] = value
            insert_values.append(f":{column}")

    update_parts = []
    for column in columns:
        if column in NO_UPDATE_COLUMNS:
            continue
        update_parts.append(f"{column} = EXCLUDED.{column}")
    update_parts.append("updated_at = NOW()")

    db.execute(
        text(
            f"""
            INSERT INTO {table} ({', '.join(columns)})
            VALUES ({', '.join(insert_values)})
            ON CONFLICT (staff_key, role_scope, window_days) DO UPDATE SET
                {', '.join(update_parts)}
            """
        ),
        params,
    )
    db.commit()
    return get_staff_action_review(
        db,
        schema,
        staff_key=str(payload.get("staff_key") or "").strip(),
        role_scope=payload.get("role_scope"),
        window_days=int(payload.get("window_days") or 7),
    ) or {}


def update_staff_action_status(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    review_id: Optional[int] = None,
    staff_key: Optional[str] = None,
    action_index: int,
    status: str,
    window_days: int = 7,
    role_scope: Optional[str] = None,
) -> dict[str, Any]:
    ensure_staff_action_intelligence_cache_table(db, schema)
    table = table_ref(schema, STAFF_ACTION_INTELLIGENCE_CACHE_TABLE)

    if review_id not in (None, ""):
        row = q1(db, f"SELECT * FROM {table} WHERE id = :review_id LIMIT 1", {"review_id": int(review_id)})
    elif staff_key not in (None, ""):
        row = q1(
            db,
            f"""
            SELECT *
            FROM {table}
            WHERE staff_key = :staff_key
              AND window_days = :window_days
              AND (:role_scope IS NULL OR COALESCE(role_scope, '') = :role_scope)
            ORDER BY rated_at DESC NULLS LAST, id DESC
            LIMIT 1
            """,
            {
                "staff_key": str(staff_key).strip(),
                "window_days": int(window_days or 7),
                "role_scope": str(role_scope).strip() if role_scope not in (None, "") else None,
            },
        )
    else:
        raise ValueError("Provide review_id or staff_key to update action status.")

    if not row:
        raise ValueError("Staff action intelligence review not found.")

    actions = parse_jsonish(row.get("recommended_actions_json"), []) or []
    updated_actions = apply_action_status_update(actions, action_index, status)
    next_status = _aggregate_review_status(updated_actions)

    db.execute(
        text(
            f"""
            UPDATE {table}
            SET recommended_actions_json = CAST(:recommended_actions_json AS jsonb),
                status = :status,
                updated_at = NOW()
            WHERE id = :review_id
            """
        ),
        {
            "recommended_actions_json": json.dumps(updated_actions, ensure_ascii=False, default=str),
            "status": next_status,
            "review_id": int(row.get("id")),
        },
    )
    db.commit()
    return get_staff_action_review_by_id(db, schema, review_id=int(row.get("id"))) or {}
