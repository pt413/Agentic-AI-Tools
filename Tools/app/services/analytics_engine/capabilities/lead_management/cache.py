from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from .common import schema_ident


LEAD_LLM_REVIEW_CACHE_TABLE = "lead_communication_review"


def ensure_lead_review_cache_table(db: Session, schema: str) -> None:
    """Create the compact one-row-per-lead review table.

    This table intentionally stores only review outputs needed by dashboards:
    overall score/priority, team/actor scorecards, actions, compact summary, and
    review text. It does not store full LLM prompt/context payloads.
    """
    table = f"{schema_ident(schema)}.{LEAD_LLM_REVIEW_CACHE_TABLE}"
    db.execute(text(f"""
    CREATE TABLE IF NOT EXISTS {table} (
        lead_id BIGINT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'ok',
        error TEXT,
        model TEXT,
        context_version TEXT,
        context_hash TEXT,
        overall_score NUMERIC,
        overall_priority_score NUMERIC,
        lead_handling_score NUMERIC,
        customer_perspective_score NUMERIC,
        overall_risk TEXT,
        post_booking_risk TEXT,
        main_reason TEXT,
        action_rows JSONB NOT NULL DEFAULT '[]'::jsonb,
        stakeholder_scores JSONB NOT NULL DEFAULT '[]'::jsonb,
        actor_scores JSONB NOT NULL DEFAULT '[]'::jsonb,
        summary JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        review_text TEXT,
        stale_at TIMESTAMP,
        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """))
    # Keep this idempotent if the table was created manually with fewer columns.
    alter_columns = {
        "status": "TEXT NOT NULL DEFAULT 'ok'",
        "error": "TEXT",
        "model": "TEXT",
        "context_version": "TEXT",
        "context_hash": "TEXT",
        "overall_score": "NUMERIC",
        "overall_priority_score": "NUMERIC",
        "lead_handling_score": "NUMERIC",
        "customer_perspective_score": "NUMERIC",
        "overall_risk": "TEXT",
        "post_booking_risk": "TEXT",
        "main_reason": "TEXT",
        "action_rows": "JSONB NOT NULL DEFAULT '[]'::jsonb",
        "stakeholder_scores": "JSONB NOT NULL DEFAULT '[]'::jsonb",
        "actor_scores": "JSONB NOT NULL DEFAULT '[]'::jsonb",
        "summary": "JSONB NOT NULL DEFAULT '{}'::jsonb",
        "review_text": "TEXT",
        "stale_at": "TIMESTAMP",
        "created_at": "TIMESTAMP NOT NULL DEFAULT NOW()",
        "updated_at": "TIMESTAMP NOT NULL DEFAULT NOW()",
    }
    for column_name, column_type in alter_columns.items():
        db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column_name} {column_type}"))

    # If an older version stored more than one row per lead, keep the latest usable row.
    db.execute(text(f"""
    DELETE FROM {table} t
    USING (
        SELECT ctid,
               ROW_NUMBER() OVER (
                   PARTITION BY lead_id
                   ORDER BY
                       CASE WHEN LOWER(COALESCE(status::text, 'ok')) = 'ok' THEN 0 ELSE 1 END,
                       updated_at DESC NULLS LAST,
                       created_at DESC NULLS LAST,
                       ctid DESC
               ) AS rn
        FROM {table}
        WHERE lead_id IS NOT NULL
    ) d
    WHERE t.ctid = d.ctid
      AND d.rn > 1
    """))

    db.execute(text(f"""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_lead_communication_review_lead_id
    ON {table} (lead_id)
    """))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_lead_communication_review_priority ON {table} (overall_priority_score DESC NULLS LAST)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_lead_communication_review_score ON {table} (overall_score ASC NULLS LAST)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_lead_communication_review_status ON {table} (status)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_lead_communication_review_risk ON {table} (overall_risk)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_lead_communication_review_updated_at ON {table} (updated_at DESC NULLS LAST)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_lead_communication_review_stale_at ON {table} (stale_at DESC NULLS LAST)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_lead_communication_review_context ON {table} (context_version, context_hash)"))
    db.commit()


