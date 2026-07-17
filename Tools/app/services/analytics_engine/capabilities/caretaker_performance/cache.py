from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.analytics_engine.capabilities.lead_management.common import schema_ident


CARETAKER_PERFORMANCE_CACHE_TABLE = "caretaker_performance_review"
CARETAKER_PERFORMANCE_DEFAULT_DAYS = 30


def _json_param(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def ensure_caretaker_performance_cache_table(db: Session, schema: str) -> None:
    table = f"{schema_ident(schema)}.{CARETAKER_PERFORMANCE_CACHE_TABLE}"

    db.execute(text(f"""
    CREATE TABLE IF NOT EXISTS {table} (
        cache_key TEXT PRIMARY KEY,

        username TEXT,
        email TEXT,
        phone TEXT,
        staff_team TEXT,
        role_scope TEXT NOT NULL DEFAULT 'caretaker',

        window_days INTEGER NOT NULL DEFAULT 30,
        window_start TIMESTAMP,
        window_end TIMESTAMP,

        status TEXT NOT NULL DEFAULT 'ok',
        error TEXT,
        model TEXT,
        context_version TEXT,
        context_hash TEXT,

        overall_score NUMERIC,
        priority_score NUMERIC,
        communication_score NUMERIC,
        site_visit_score NUMERIC,
        ticket_score NUMERIC,
        management_score NUMERIC,
        overall_risk TEXT,
        main_reason TEXT,

        summary JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        metrics JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        action_rows JSONB NOT NULL DEFAULT '[]'::jsonb,
        review_text TEXT,

        stale_at TIMESTAMP,
        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """))

    # Migration safety for older existing tables.
    # CREATE TABLE IF NOT EXISTS does not add newly introduced columns.
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS username TEXT"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS email TEXT"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS phone TEXT"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS staff_team TEXT"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS role_scope TEXT NOT NULL DEFAULT 'caretaker'"))

    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS window_days INTEGER NOT NULL DEFAULT 30"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS window_start TIMESTAMP"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS window_end TIMESTAMP"))

    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ok'"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS error TEXT"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS model TEXT"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS context_version TEXT"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS context_hash TEXT"))

    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS overall_score NUMERIC"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS priority_score NUMERIC"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS communication_score NUMERIC"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS site_visit_score NUMERIC"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS ticket_score NUMERIC"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS management_score NUMERIC"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS overall_risk TEXT"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS main_reason TEXT"))

    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS summary JSONB NOT NULL DEFAULT '{{}}'::jsonb"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS metrics JSONB NOT NULL DEFAULT '{{}}'::jsonb"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS action_rows JSONB NOT NULL DEFAULT '[]'::jsonb"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS review_text TEXT"))

    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS stale_at TIMESTAMP"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT NOW()"))
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW()"))

    # Keep defaults aligned with the current 30-day business dashboard.
    db.execute(text(f"ALTER TABLE {table} ALTER COLUMN role_scope SET DEFAULT 'caretaker'"))
    db.execute(text(f"ALTER TABLE {table} ALTER COLUMN window_days SET DEFAULT 30"))
    db.execute(text(f"ALTER TABLE {table} ALTER COLUMN status SET DEFAULT 'ok'"))
    db.execute(text(f"ALTER TABLE {table} ALTER COLUMN summary SET DEFAULT '{{}}'::jsonb"))
    db.execute(text(f"ALTER TABLE {table} ALTER COLUMN metrics SET DEFAULT '{{}}'::jsonb"))
    db.execute(text(f"ALTER TABLE {table} ALTER COLUMN action_rows SET DEFAULT '[]'::jsonb"))
    db.execute(text(f"ALTER TABLE {table} ALTER COLUMN created_at SET DEFAULT NOW()"))
    db.execute(text(f"ALTER TABLE {table} ALTER COLUMN updated_at SET DEFAULT NOW()"))

    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_caretaker_perf_username ON {table} (username)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_caretaker_perf_phone ON {table} (phone)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_caretaker_perf_score ON {table} (overall_score ASC NULLS LAST)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_caretaker_perf_priority ON {table} (priority_score DESC NULLS LAST)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_caretaker_perf_risk ON {table} (overall_risk)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_caretaker_perf_updated ON {table} (updated_at DESC NULLS LAST)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_caretaker_perf_stale ON {table} (stale_at DESC NULLS LAST)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_caretaker_perf_window ON {table} (window_days)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_caretaker_perf_status_stale ON {table} (status, stale_at)"))

    db.commit()


def make_cache_key(
    *,
    username: Any = None,
    phone: Any = None,
    email: Any = None,
    days: int = CARETAKER_PERFORMANCE_DEFAULT_DAYS,
) -> str:
    identity = str(username or email or phone or "").strip().lower()
    if not identity:
        raise ValueError("Cannot build caretaker performance cache key without username, email, or phone")

    safe_days = int(days or CARETAKER_PERFORMANCE_DEFAULT_DAYS)
    return f"caretaker:{identity}:days:{safe_days}"


def get_caretaker_performance_cache(
    db: Session,
    schema: str,
    *,
    cache_key: str,
    ok_only: bool = True,
) -> dict[str, Any] | None:
    ensure_caretaker_performance_cache_table(db, schema)
    table = f"{schema_ident(schema)}.{CARETAKER_PERFORMANCE_CACHE_TABLE}"

    where = "cache_key = :cache_key"
    if ok_only:
        where += " AND LOWER(COALESCE(status, '')) = 'ok' AND stale_at IS NULL"

    row = db.execute(
        text(f"""
        SELECT *
        FROM {table}
        WHERE {where}
        LIMIT 1
        """),
        {"cache_key": cache_key},
    ).mappings().first()

    return dict(row) if row else None


def store_caretaker_performance_cache(
    db: Session,
    schema: str,
    *,
    payload: dict[str, Any],
) -> None:
    ensure_caretaker_performance_cache_table(db, schema)
    table = f"{schema_ident(schema)}.{CARETAKER_PERFORMANCE_CACHE_TABLE}"

    db.execute(
        text(f"""
        INSERT INTO {table} (
            cache_key,
            username, email, phone, staff_team, role_scope,
            window_days, window_start, window_end,
            status, error, model, context_version, context_hash,
            overall_score, priority_score, communication_score, site_visit_score,
            ticket_score, management_score, overall_risk, main_reason,
            summary, metrics, action_rows, review_text,
            stale_at, updated_at
        ) VALUES (
            :cache_key,
            :username, :email, :phone, :staff_team, :role_scope,
            :window_days, :window_start, :window_end,
            :status, :error, :model, :context_version, :context_hash,
            :overall_score, :priority_score, :communication_score, :site_visit_score,
            :ticket_score, :management_score, :overall_risk, :main_reason,
            CAST(:summary AS jsonb), CAST(:metrics AS jsonb), CAST(:action_rows AS jsonb), :review_text,
            NULL, NOW()
        )
        ON CONFLICT (cache_key) DO UPDATE SET
            username = EXCLUDED.username,
            email = EXCLUDED.email,
            phone = EXCLUDED.phone,
            staff_team = EXCLUDED.staff_team,
            role_scope = EXCLUDED.role_scope,
            window_days = EXCLUDED.window_days,
            window_start = EXCLUDED.window_start,
            window_end = EXCLUDED.window_end,
            status = EXCLUDED.status,
            error = EXCLUDED.error,
            model = EXCLUDED.model,
            context_version = EXCLUDED.context_version,
            context_hash = EXCLUDED.context_hash,
            overall_score = EXCLUDED.overall_score,
            priority_score = EXCLUDED.priority_score,
            communication_score = EXCLUDED.communication_score,
            site_visit_score = EXCLUDED.site_visit_score,
            ticket_score = EXCLUDED.ticket_score,
            management_score = EXCLUDED.management_score,
            overall_risk = EXCLUDED.overall_risk,
            main_reason = EXCLUDED.main_reason,
            summary = EXCLUDED.summary,
            metrics = EXCLUDED.metrics,
            action_rows = EXCLUDED.action_rows,
            review_text = EXCLUDED.review_text,
            stale_at = NULL,
            updated_at = NOW()
        """),
        {
            "cache_key": payload.get("cache_key"),
            "username": payload.get("username"),
            "email": payload.get("email"),
            "phone": payload.get("phone"),
            "staff_team": payload.get("staff_team"),
            "role_scope": payload.get("role_scope") or "caretaker",
            "window_days": int(payload.get("window_days") or CARETAKER_PERFORMANCE_DEFAULT_DAYS),
            "window_start": payload.get("window_start"),
            "window_end": payload.get("window_end"),
            "status": payload.get("status") or "ok",
            "error": payload.get("error"),
            "model": payload.get("model"),
            "context_version": payload.get("context_version"),
            "context_hash": payload.get("context_hash"),
            "overall_score": payload.get("overall_score"),
            "priority_score": payload.get("priority_score"),
            "communication_score": payload.get("communication_score"),
            "site_visit_score": payload.get("site_visit_score"),
            "ticket_score": payload.get("ticket_score"),
            "management_score": payload.get("management_score"),
            "overall_risk": payload.get("overall_risk"),
            "main_reason": payload.get("main_reason"),
            "summary": _json_param(payload.get("summary") or {}),
            "metrics": _json_param(payload.get("metrics") or {}),
            "action_rows": _json_param(payload.get("action_rows") or []),
            "review_text": payload.get("review_text"),
        },
    )
    db.commit()


def mark_caretaker_performance_stale(
    db: Session,
    schema: str,
    *,
    cache_key: str,
    reason: str = "caretaker evidence changed",
) -> dict[str, Any]:
    ensure_caretaker_performance_cache_table(db, schema)
    table = f"{schema_ident(schema)}.{CARETAKER_PERFORMANCE_CACHE_TABLE}"

    result = db.execute(
        text(f"""
        UPDATE {table}
        SET status = 'stale',
            stale_at = NOW(),
            error = :reason,
            updated_at = NOW()
        WHERE cache_key = :cache_key
        """),
        {"cache_key": cache_key, "reason": reason},
    )
    db.commit()

    return {"status": "ok", "updated": int(result.rowcount or 0), "cache_key": cache_key}