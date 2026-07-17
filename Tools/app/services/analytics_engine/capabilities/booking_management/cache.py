from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


# Prevent dashboard/API requests from running DDL/index/migration checks on every call.
# This is process-local; restarting FastAPI will run the ensure once again.
_BOOKING_REVIEW_CACHE_ENSURED: set[str] = set()

from .common import (  # noqa: E402
    BOOKING_LLM_REVIEW_CACHE_TABLE,
    BOOKING_REVIEW_CONTEXT_VERSION,
    DEFAULT_SCHEMA,
    LEGACY_BOOKING_FOLLOWUP_CACHE_TABLE,
    compact_dict,
    json_param,
    parse_jsonish,
    q1,
    table_exists,
    table_ref,
    schema_ident,
)


# Keep the existing one-row-per-booking cache table as the source of truth.
# These added columns make the cache directly usable for manager/caretaker/building
# queues without repeatedly joining staging tables on every dashboard read.
BOOKING_REVIEW_CACHE_COLUMNS: dict[str, str] = {
    # core cache status/output
    "status": "TEXT NOT NULL DEFAULT 'ok'",
    "error": "TEXT",
    "model": "TEXT",
    "context_version": "TEXT",
    "context_hash": "TEXT",
    "overall_score": "NUMERIC",
    "overall_priority_score": "NUMERIC",
    "customer_perspective_score": "NUMERIC",
    "operations_score": "NUMERIC",
    "support_score": "NUMERIC",
    "overall_risk": "TEXT",
    "onboarding_risk": "TEXT",
    "support_risk": "TEXT",
    "main_reason": "TEXT",
    "action_rows": "JSONB NOT NULL DEFAULT '[]'::jsonb",
    "stakeholder_scores": "JSONB NOT NULL DEFAULT '[]'::jsonb",
    "actor_scores": "JSONB NOT NULL DEFAULT '[]'::jsonb",
    "summary": "JSONB NOT NULL DEFAULT '{}'::jsonb",
    "review_text": "TEXT",

    # prompt/run metadata
    "review_window_days": "INT",
    "review_generated_at": "TIMESTAMP",

    # denormalized booking/property/building scope
    "booking_status": "TEXT",
    "stay_state": "TEXT",
    "booking_type": "TEXT",
    "property_id": "BIGINT",
    "property_name": "TEXT",
    "building_id": "BIGINT",
    "building_name": "TEXT",
    "travel_from_date": "DATE",
    "travel_to_date": "DATE",

    # denormalized ownership scope
    "sales_owner": "TEXT",
    "ops_owner": "TEXT",
    "ops_manager": "TEXT",
    "caretaker": "TEXT",
    "finance_owner": "TEXT",
    "finance_manager": "TEXT",
    "support_owner": "TEXT",

    # operational work queue fields derived from action_rows
    "work_priority_score": "NUMERIC",
    "work_owner_team": "TEXT",
    "work_action": "TEXT",
    "work_evidence": "TEXT",
    "is_no_action": "BOOLEAN NOT NULL DEFAULT FALSE",
    "action_owner_teams": "JSONB NOT NULL DEFAULT '[]'::jsonb",
    "issue_themes": "JSONB NOT NULL DEFAULT '[]'::jsonb",

    # timestamps
    "stale_at": "TIMESTAMP",
    "created_at": "TIMESTAMP NOT NULL DEFAULT NOW()",
    "updated_at": "TIMESTAMP NOT NULL DEFAULT NOW()",
}


BOOKING_REVIEW_CACHE_INSERT_COLUMNS = [
    "booking_id",
    "status",
    "error",
    "model",
    "context_version",
    "context_hash",
    "overall_score",
    "overall_priority_score",
    "customer_perspective_score",
    "operations_score",
    "support_score",
    "overall_risk",
    "onboarding_risk",
    "support_risk",
    "main_reason",
    "action_rows",
    "stakeholder_scores",
    "actor_scores",
    "summary",
    "review_text",
    "review_window_days",
    "review_generated_at",
    "booking_status",
    "stay_state",
    "booking_type",
    "property_id",
    "property_name",
    "building_id",
    "building_name",
    "travel_from_date",
    "travel_to_date",
    "sales_owner",
    "ops_owner",
    "ops_manager",
    "caretaker",
    "finance_owner",
    "finance_manager",
    "support_owner",
    "work_priority_score",
    "work_owner_team",
    "work_action",
    "work_evidence",
    "is_no_action",
    "action_owner_teams",
    "issue_themes",
    "stale_at",
    "updated_at",
]

JSONB_INSERT_COLUMNS = {"action_rows", "stakeholder_scores", "actor_scores", "summary", "action_owner_teams", "issue_themes"}
NO_UPDATE_COLUMNS = {"booking_id", "created_at"}


def ensure_booking_review_cache_table(db: Session, schema: str = DEFAULT_SCHEMA, *, force: bool = False) -> None:
    """Ensure one-row-per-booking review cache table exists.

    Dashboard reads from this table and never calls the LLM.  The table now also
    stores enough scope/work columns to support ops-manager, finance, caretaker,
    and building-level queues from the same booking-level review row.
    """
    ensure_key = str(schema or DEFAULT_SCHEMA)
    if not force and ensure_key in _BOOKING_REVIEW_CACHE_ENSURED:
        return

    table = table_ref(schema, BOOKING_LLM_REVIEW_CACHE_TABLE)
    db.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_ident(schema)}"))
    db.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                booking_id BIGINT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'ok',
                error TEXT,
                model TEXT,
                context_version TEXT,
                context_hash TEXT,
                overall_score NUMERIC,
                overall_priority_score NUMERIC,
                customer_perspective_score NUMERIC,
                operations_score NUMERIC,
                support_score NUMERIC,
                overall_risk TEXT,
                onboarding_risk TEXT,
                support_risk TEXT,
                main_reason TEXT,
                action_rows JSONB NOT NULL DEFAULT '[]'::jsonb,
                stakeholder_scores JSONB NOT NULL DEFAULT '[]'::jsonb,
                actor_scores JSONB NOT NULL DEFAULT '[]'::jsonb,
                summary JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                review_text TEXT,
                review_window_days INT,
                review_generated_at TIMESTAMP,
                booking_status TEXT,
                stay_state TEXT,
                booking_type TEXT,
                property_id BIGINT,
                property_name TEXT,
                building_id BIGINT,
                building_name TEXT,
                travel_from_date DATE,
                travel_to_date DATE,
                sales_owner TEXT,
                ops_owner TEXT,
                ops_manager TEXT,
                caretaker TEXT,
                finance_owner TEXT,
                finance_manager TEXT,
                support_owner TEXT,
                work_priority_score NUMERIC,
                work_owner_team TEXT,
                work_action TEXT,
                work_evidence TEXT,
                is_no_action BOOLEAN NOT NULL DEFAULT FALSE,
                action_owner_teams JSONB NOT NULL DEFAULT '[]'::jsonb,
                issue_themes JSONB NOT NULL DEFAULT '[]'::jsonb,
                stale_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
    )

    for column_name, column_type in BOOKING_REVIEW_CACHE_COLUMNS.items():
        db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column_name} {column_type}"))

    db.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS ux_booking_communication_review_booking_id ON {table} (booking_id)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_booking_communication_review_priority ON {table} (overall_priority_score DESC NULLS LAST)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_booking_communication_review_work_priority ON {table} (work_priority_score DESC NULLS LAST)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_booking_communication_review_score ON {table} (overall_score ASC NULLS LAST)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_booking_communication_review_status ON {table} (status)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_booking_communication_review_risk ON {table} (overall_risk)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_booking_communication_review_ops_manager ON {table} (ops_manager)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_booking_communication_review_caretaker ON {table} (caretaker)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_booking_communication_review_finance_manager ON {table} (finance_manager)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_booking_communication_review_building ON {table} (building_id)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_booking_communication_review_stay_dates ON {table} (travel_from_date, travel_to_date)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_booking_communication_review_stale_at ON {table} (stale_at DESC NULLS LAST)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_booking_communication_review_updated_at ON {table} (updated_at DESC NULLS LAST)"))
    db.commit()
    migrate_legacy_booking_followup_cache(db, schema)
    _BOOKING_REVIEW_CACHE_ENSURED.add(ensure_key)


def migrate_legacy_booking_followup_cache(db: Session, schema: str = DEFAULT_SCHEMA) -> int:
    """Copy latest old follow-up cache rows into new one-row dashboard cache.

    This is best-effort and only runs when the legacy table exists.
    """
    if not table_exists(db, schema, LEGACY_BOOKING_FOLLOWUP_CACHE_TABLE):
        return 0
    table = table_ref(schema, BOOKING_LLM_REVIEW_CACHE_TABLE)
    legacy = table_ref(schema, LEGACY_BOOKING_FOLLOWUP_CACHE_TABLE)
    result = db.execute(
        text(
            f"""
            WITH latest AS (
                SELECT DISTINCT ON (booking_id)
                    booking_id,
                    COALESCE(status, 'ok') AS status,
                    error,
                    model,
                    context_version,
                    context_hash,
                    score AS overall_score,
                    customer_perspective_score,
                    risk AS overall_risk,
                    main_reason,
                    COALESCE(action_rows, '[]'::jsonb) AS action_rows,
                    COALESCE(stakeholder_scores, '[]'::jsonb) AS stakeholder_scores,
                    COALESCE(summary, '{{}}'::jsonb) AS summary,
                    review_text,
                    created_at,
                    updated_at
                FROM {legacy}
                WHERE booking_id IS NOT NULL
                ORDER BY booking_id, updated_at DESC NULLS LAST, id DESC NULLS LAST
            )
            INSERT INTO {table} (
                booking_id, status, error, model, context_version, context_hash,
                overall_score, customer_perspective_score, overall_risk, main_reason,
                action_rows, stakeholder_scores, summary, review_text, created_at, updated_at
            )
            SELECT
                booking_id, status, error, model, context_version, context_hash,
                overall_score, customer_perspective_score, overall_risk, main_reason,
                action_rows, stakeholder_scores, summary, review_text,
                COALESCE(created_at, NOW()), COALESCE(updated_at, NOW())
            FROM latest
            ON CONFLICT (booking_id) DO NOTHING
            """
        )
    )
    db.commit()
    return int(result.rowcount or 0)


def _cache_row_to_payload(row: dict[str, Any]) -> dict[str, Any]:
    action_rows = parse_jsonish(row.get("action_rows"), []) or []
    stakeholder_scores = parse_jsonish(row.get("stakeholder_scores"), []) or []
    actor_scores = parse_jsonish(row.get("actor_scores"), []) or []
    summary = parse_jsonish(row.get("summary"), {}) or {}
    action_owner_teams = parse_jsonish(row.get("action_owner_teams"), []) or []
    issue_themes = parse_jsonish(row.get("issue_themes"), []) or []
    return compact_dict(
        {
            "view": "booking_llm_rating",
            "booking_id": row.get("booking_id"),
            "status": row.get("status"),
            "error": row.get("error"),
            "model": row.get("model"),
            "context_version": row.get("context_version"),
            "context_hash": row.get("context_hash"),
            "overall_score": row.get("overall_score"),
            "overall_priority_score": row.get("overall_priority_score"),
            "work_priority_score": row.get("work_priority_score"),
            "customer_perspective_score": row.get("customer_perspective_score"),
            "operations_score": row.get("operations_score"),
            "support_score": row.get("support_score"),
            "overall_risk": row.get("overall_risk"),
            "onboarding_risk": row.get("onboarding_risk"),
            "support_risk": row.get("support_risk"),
            "main_reason": row.get("main_reason"),
            "work_owner_team": row.get("work_owner_team"),
            "work_action": row.get("work_action"),
            "work_evidence": row.get("work_evidence"),
            "is_no_action": row.get("is_no_action"),
            "action_owner_teams": action_owner_teams,
            "issue_themes": issue_themes,
            "action_rows": action_rows,
            "stakeholder_scores": stakeholder_scores,
            "actor_scores": actor_scores,
            "booking_summary": summary,
            "summary": summary,
            "booking": compact_dict(
                {
                    "status": row.get("booking_status"),
                    "state": row.get("stay_state"),
                    "type": row.get("booking_type"),
                    "property_id": row.get("property_id"),
                    "property_name": row.get("property_name"),
                    "building_id": row.get("building_id"),
                    "building_name": row.get("building_name"),
                    "travel_from_date": row.get("travel_from_date"),
                    "travel_to_date": row.get("travel_to_date"),
                }
            ),
            "stakeholders": compact_dict(
                {
                    "sales_owner": row.get("sales_owner"),
                    "ops_owner": row.get("ops_owner"),
                    "ops_manager": row.get("ops_manager"),
                    "caretaker": row.get("caretaker"),
                    "finance_owner": row.get("finance_owner"),
                    "finance_manager": row.get("finance_manager"),
                    "support_owner": row.get("support_owner"),
                    "scorecard": stakeholder_scores,
                }
            ),
            "review_window_days": row.get("review_window_days"),
            "review_generated_at": row.get("review_generated_at"),
            "review_text": row.get("review_text"),
            "stale_at": row.get("stale_at"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }
    )


def get_booking_review_cache(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    booking_id: int,
    ok_only: bool = False,
) -> Optional[dict[str, Any]]:
    ensure_booking_review_cache_table(db, schema)
    table = table_ref(schema, BOOKING_LLM_REVIEW_CACHE_TABLE)
    status_filter = "AND LOWER(COALESCE(status, '')) = 'ok' AND stale_at IS NULL" if ok_only else ""
    row = q1(
        db,
        f"""
        SELECT *
        FROM {table}
        WHERE booking_id = :booking_id
        {status_filter}
        LIMIT 1
        """,
        {"booking_id": int(booking_id)},
    )
    return _cache_row_to_payload(row) if row else None


def _insert_value_sql(column_name: str) -> str:
    if column_name in JSONB_INSERT_COLUMNS:
        return f"CAST(:{column_name} AS jsonb)"
    if column_name == "updated_at":
        return "NOW()"
    if column_name == "stale_at":
        return "NULL"
    return f":{column_name}"


def store_booking_review_cache(db: Session, schema: str = DEFAULT_SCHEMA, *, payload: dict[str, Any]) -> None:
    ensure_booking_review_cache_table(db, schema)
    table = table_ref(schema, BOOKING_LLM_REVIEW_CACHE_TABLE)
    status = payload.get("status") or "ok"

    insert_columns_sql = ", ".join(BOOKING_REVIEW_CACHE_INSERT_COLUMNS)
    insert_values_sql = ", ".join(_insert_value_sql(column) for column in BOOKING_REVIEW_CACHE_INSERT_COLUMNS)
    update_assignments_sql = ",\n                ".join(
        f"{column} = EXCLUDED.{column}"
        for column in BOOKING_REVIEW_CACHE_INSERT_COLUMNS
        if column not in NO_UPDATE_COLUMNS and column not in {"updated_at", "stale_at"}
    )

    db.execute(
        text(
            f"""
            INSERT INTO {table} ({insert_columns_sql})
            VALUES ({insert_values_sql})
            ON CONFLICT (booking_id) DO UPDATE SET
                {update_assignments_sql},
                stale_at = NULL,
                updated_at = NOW()
            WHERE EXCLUDED.status = 'ok'
               OR {table}.status IS DISTINCT FROM 'ok'
               OR {table}.stale_at IS NOT NULL
            """
        ),
        {
            "booking_id": int(payload["booking_id"]),
            "status": status,
            "error": payload.get("error"),
            "model": payload.get("model"),
            "context_version": payload.get("context_version") or BOOKING_REVIEW_CONTEXT_VERSION,
            "context_hash": payload.get("context_hash"),
            "overall_score": payload.get("overall_score"),
            "overall_priority_score": payload.get("overall_priority_score"),
            "customer_perspective_score": payload.get("customer_perspective_score"),
            "operations_score": payload.get("operations_score"),
            "support_score": payload.get("support_score"),
            "overall_risk": payload.get("overall_risk"),
            "onboarding_risk": payload.get("onboarding_risk"),
            "support_risk": payload.get("support_risk"),
            "main_reason": payload.get("main_reason"),
            "action_rows": json_param(payload.get("action_rows") or []),
            "stakeholder_scores": json_param(payload.get("stakeholder_scores") or []),
            "actor_scores": json_param(payload.get("actor_scores") or []),
            "summary": json_param(payload.get("booking_summary") or payload.get("summary") or {}),
            "review_text": payload.get("review_text"),
            "review_window_days": payload.get("review_window_days"),
            "review_generated_at": payload.get("review_generated_at"),
            "booking_status": payload.get("booking_status"),
            "stay_state": payload.get("stay_state"),
            "booking_type": payload.get("booking_type"),
            "property_id": payload.get("property_id"),
            "property_name": payload.get("property_name"),
            "building_id": payload.get("building_id"),
            "building_name": payload.get("building_name"),
            "travel_from_date": payload.get("travel_from_date"),
            "travel_to_date": payload.get("travel_to_date"),
            "sales_owner": payload.get("sales_owner"),
            "ops_owner": payload.get("ops_owner"),
            "ops_manager": payload.get("ops_manager"),
            "caretaker": payload.get("caretaker"),
            "finance_owner": payload.get("finance_owner"),
            "finance_manager": payload.get("finance_manager"),
            "support_owner": payload.get("support_owner"),
            "work_priority_score": payload.get("work_priority_score"),
            "work_owner_team": payload.get("work_owner_team"),
            "work_action": payload.get("work_action"),
            "work_evidence": payload.get("work_evidence"),
            "is_no_action": bool(payload.get("is_no_action", False)),
            "action_owner_teams": json_param(payload.get("action_owner_teams") or []),
            "issue_themes": json_param(payload.get("issue_themes") or []),
        },
    )
    db.commit()


def mark_booking_review_stale(db: Session, schema: str = DEFAULT_SCHEMA, *, booking_id: int, reason: str | None = None) -> None:
    ensure_booking_review_cache_table(db, schema)
    table = table_ref(schema, BOOKING_LLM_REVIEW_CACHE_TABLE)
    db.execute(
        text(
            f"""
            INSERT INTO {table} (booking_id, status, error, stale_at, updated_at)
            VALUES (:booking_id, 'stale', :reason, NOW(), NOW())
            ON CONFLICT (booking_id) DO UPDATE SET
                status = 'stale',
                error = COALESCE(:reason, {table}.error),
                stale_at = NOW(),
                updated_at = NOW()
            """
        ),
        {"booking_id": int(booking_id), "reason": reason or "marked stale"},
    )
    db.commit()
