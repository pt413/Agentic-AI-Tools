from __future__ import annotations

from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .cache import LEAD_LLM_REVIEW_CACHE_TABLE, ensure_lead_review_cache_table
from .common import schema_ident
from .rating_runner import LeadCommunicationReviewRunner


def _lead_id_params(lead_ids: Iterable[int]) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {}
    holders: list[str] = []
    for idx, value in enumerate(dict.fromkeys(int(v) for v in lead_ids if v not in (None, ""))):
        key = f"lead_id_{idx}"
        holders.append(f":{key}")
        params[key] = value
    return ", ".join(holders), params


def mark_leads_stale(
    db: Session,
    schema: str,
    lead_ids: Iterable[int],
    *,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """Mark cached lead ratings stale after new lead communication/activity."""
    ensure_lead_review_cache_table(db, schema)
    holders, params = _lead_id_params(lead_ids)
    if not holders:
        return {"updated": 0, "lead_ids": []}

    table = f"{schema_ident(schema)}.{LEAD_LLM_REVIEW_CACHE_TABLE}"
    params["reason"] = reason or "stale_due_to_new_lead_activity"
    result = db.execute(
        text(
            f"""
            UPDATE {table}
            SET status = 'stale',
                stale_at = NOW(),
                error = :reason,
                updated_at = NOW()
            WHERE lead_id IN ({holders})
            """
        ),
        params,
    )
    db.commit()
    return {"updated": int(result.rowcount or 0), "lead_ids": list(params.values())[:-1]}


def list_stale_lead_ids(db: Session, schema: str, *, limit: int = 100) -> list[int]:
    """Return lead IDs that should be recomputed by cron."""
    ensure_lead_review_cache_table(db, schema)
    table = f"{schema_ident(schema)}.{LEAD_LLM_REVIEW_CACHE_TABLE}"
    rows = db.execute(
        text(
            f"""
            SELECT lead_id
            FROM {table}
            WHERE LOWER(COALESCE(status::text, '')) <> 'ok'
               OR stale_at IS NOT NULL
            ORDER BY stale_at ASC NULLS FIRST, updated_at ASC NULLS FIRST, lead_id ASC
            LIMIT :limit_n
            """
        ),
        {"limit_n": int(limit)},
    ).mappings().fetchall()
    return [int(row["lead_id"]) for row in rows if row.get("lead_id") is not None]


def recompute_stale_leads(
    db: Session,
    schema: str,
    *,
    limit: int = 50,
    days: int = 90,
    model: str = "builtin",
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Cron helper: recompute stale ratings and store fresh ok rows."""
    lead_ids = list_stale_lead_ids(db, schema, limit=limit)
    runner = LeadCommunicationReviewRunner(db=db, schema=schema)
    results: list[dict[str, Any]] = []
    for lead_id in lead_ids:
        result = runner.review_one(
            lead_id=lead_id,
            days=days,
            run_llm=True,
            model=model,
            timeout_seconds=timeout_seconds,
            use_cache=True,
            force_refresh=True,
        )
        results.append(
            {
                "lead_id": lead_id,
                "status": result.get("status"),
                "cached": result.get("cached"),
                "cache_status": result.get("cache_status"),
                "overall_score": result.get("overall_score"),
                "overall_priority_score": result.get("overall_priority_score"),
                "error": result.get("error"),
            }
        )
    return {"requested": len(lead_ids), "results": results}
