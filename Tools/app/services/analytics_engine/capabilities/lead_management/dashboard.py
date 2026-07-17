from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .cache import LEAD_LLM_REVIEW_CACHE_TABLE, ensure_lead_review_cache_table
from .common import (
    DEFAULT_SCHEMA,
    LEAD_REVIEW_CONTEXT_VERSION,
    compact_dict,
    q,
    schema_ident,
    table_columns,
    table_exists,
)
from .parsing import _score_number


def _safe_sql_ident(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(value or "")):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return str(value)


def _text_col(alias: str, column_name: str) -> str:
    return f"NULLIF(TRIM({alias}.{_safe_sql_ident(column_name)}::text), '')"


def _existing_text_expr(alias: str, columns: set[str], candidates: List[str], *, default: str = "NULL") -> str:
    parts = [_text_col(alias, column) for column in candidates if column in columns]
    if not parts:
        return default
    if len(parts) == 1:
        return parts[0]
    return f"COALESCE({', '.join(parts)})"


def _jsonb_text_expr(json_alias: str, key: str) -> str:
    safe_key = str(key).replace("'", "''")
    return f"NULLIF(TRIM({json_alias}->>'{safe_key}'), '')"


def _dashboard_top_action(action_rows: Any) -> Dict[str, Any]:
    rows = action_rows if isinstance(action_rows, list) else []
    if not rows:
        return {}

    def priority_value(item: Any) -> float:
        if not isinstance(item, dict):
            return -1
        score = _score_number(item.get("priority_score"))
        try:
            return float(score) if score is not None else -1
        except Exception:
            return -1

    top = max((row for row in rows if isinstance(row, dict)), key=priority_value, default={})
    return compact_dict({
        "priority_score": top.get("priority_score"),
        "owner_team": top.get("owner_team") or top.get("owner"),
        "action": top.get("action"),
        "evidence": top.get("evidence"),
    })


def _dashboard_count(summary: Dict[str, Any], key: str) -> Any:
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    if key == "events":
        return summary.get("event_count") if summary.get("event_count") is not None else counts.get("events")
    return counts.get(key)


def list_lead_dashboard_rows(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    lead_id: Optional[int] = None,
    handled_by: Optional[str] = None,
    status: Optional[str] = None,
    source: Optional[str] = None,
    risk: Optional[str] = None,
    review_status: Optional[str] = None,
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
    min_priority: Optional[float] = None,
    max_priority: Optional[float] = None,
    search: Optional[str] = None,
    only_rated: bool = True,
    include_actions: bool = True,
    sort_by: str = "priority",
    sort_dir: str = "desc",
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    """Return compact lead dashboard rows.

    Intended for the lead dashboard page: one row per lead, basic lead fields +
    cached LLM rating. It does not call the LLM. Ratings are read from
    lead_communication_review and can be refreshed by the existing per-lead API
    or a cron job.
    """
    ensure_lead_review_cache_table(db, schema)

    if not table_exists(db, schema, "staging_lead_tracking"):
        raise ValueError("staging_lead_tracking table is required for lead dashboard")

    lead_columns = table_columns(db, schema, "staging_lead_tracking")
    if "source_id" not in lead_columns:
        raise ValueError("staging_lead_tracking.source_id is required for lead dashboard")

    lead_table = f"{schema_ident(schema)}.staging_lead_tracking"
    cache_table = f"{schema_ident(schema)}.{LEAD_LLM_REVIEW_CACHE_TABLE}"

    lead_status_expr = _existing_text_expr("l", lead_columns, ["raw_status", "lead_status", "status"])
    lead_source_expr = _existing_text_expr("l", lead_columns, ["origin", "source", "lead_source"])
    handled_by_expr = (
        f"COALESCE({_jsonb_text_expr('r.summary', 'owner')}, "
        f"{_existing_text_expr('l', lead_columns, ['executive_id', 'assigned_to', 'added_by', 'generated_by', 'actor_id'])})"
    )
    created_at_expr = "l.created_at" if "created_at" in lead_columns else "NULL"
    closed_at_expr = "l.closed_at" if "closed_at" in lead_columns else "NULL"
    synced_at_expr = "l.synced_at" if "synced_at" in lead_columns else "NULL"
    booking_id_expr = _existing_text_expr("l", lead_columns, ["booking_id"])
    user_id_expr = _existing_text_expr("l", lead_columns, ["user_id"])
    phone_expr = _existing_text_expr("l", lead_columns, ["contact_number", "contact_number_alt", "phone", "mobile"])
    email_expr = _existing_text_expr("l", lead_columns, ["email", "contact_email"])

    lvs_join_sql = ""
    lvs_customer_name_expr = "NULL"
    lvs_property_name_expr = "NULL"
    lvs_last_activity_expr = "NULL"
    lvs_next_followup_expr = "NULL"
    if table_exists(db, schema, "lead_view_summary"):
        lvs_columns = table_columns(db, schema, "lead_view_summary")
        lvs_key = "lead_id" if "lead_id" in lvs_columns else "source_id" if "source_id" in lvs_columns else None
        if lvs_key:
            lvs_table = f"{schema_ident(schema)}.lead_view_summary"
            lvs_order_expr_raw = _existing_text_expr(
                "lvs_raw",
                lvs_columns,
                ["last_activity_at", "latest_activity_at", "last_communication_at", "updated_at", "created_at", "synced_at"],
                default=f"lvs_raw.{_safe_sql_ident(lvs_key)}::text",
            )
            lvs_join_sql = f"""
            LEFT JOIN (
                SELECT *
                FROM (
                    SELECT
                        lvs_raw.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY lvs_raw.{_safe_sql_ident(lvs_key)}::text
                            ORDER BY {lvs_order_expr_raw} DESC NULLS LAST
                        ) AS _rn
                    FROM {lvs_table} lvs_raw
                ) lvs_ranked
                WHERE _rn = 1
            ) lvs ON lvs.{_safe_sql_ident(lvs_key)}::text = l.source_id::text
            """
            lvs_customer_name_expr = _existing_text_expr("lvs", lvs_columns, ["customer_name", "lead_name", "name", "traveller_name"])
            lvs_property_name_expr = _existing_text_expr("lvs", lvs_columns, ["property_name", "unit_name", "building_name", "interested_property", "preferred_property"])
            lvs_last_activity_expr = _existing_text_expr("lvs", lvs_columns, ["last_activity_at", "latest_activity_at", "last_communication_at", "updated_at", "synced_at"])
            lvs_next_followup_expr = _existing_text_expr("lvs", lvs_columns, ["next_followup_at", "followup_at", "follow_up_at", "next_call_at", "next_action_at"])

    where_parts: List[str] = []
    params: Dict[str, Any] = {
        "limit_n": max(1, min(int(limit or 100), 1000)),
        "offset_n": max(0, int(offset or 0)),
    }

    if only_rated:
        where_parts.append("r.lead_id IS NOT NULL")
    if lead_id is not None:
        where_parts.append("l.source_id = :lead_id")
        params["lead_id"] = int(lead_id)
    if handled_by:
        where_parts.append(f"LOWER({handled_by_expr}) LIKE :handled_by")
        params["handled_by"] = f"%{str(handled_by).strip().lower()}%"
    if status:
        where_parts.append(f"LOWER({lead_status_expr}) = :lead_status")
        params["lead_status"] = str(status).strip().lower()
    if source:
        where_parts.append(f"LOWER({lead_source_expr}) = :lead_source")
        params["lead_source"] = str(source).strip().lower()
    if risk:
        where_parts.append("LOWER(COALESCE(r.overall_risk, '')) = :risk")
        params["risk"] = str(risk).strip().lower()
    if review_status:
        where_parts.append("LOWER(COALESCE(r.status, 'unrated')) = :review_status")
        params["review_status"] = str(review_status).strip().lower()
    if min_score is not None:
        where_parts.append("r.overall_score >= :min_score")
        params["min_score"] = float(min_score)
    if max_score is not None:
        where_parts.append("r.overall_score <= :max_score")
        params["max_score"] = float(max_score)
    if min_priority is not None:
        where_parts.append("r.overall_priority_score >= :min_priority")
        params["min_priority"] = float(min_priority)
    if max_priority is not None:
        where_parts.append("r.overall_priority_score <= :max_priority")
        params["max_priority"] = float(max_priority)
    if search:
        where_parts.append(
            "("
            "l.source_id::text ILIKE :search OR "
            f"COALESCE({handled_by_expr}, '') ILIKE :search OR "
            f"COALESCE({lead_status_expr}, '') ILIKE :search OR "
            f"COALESCE({lead_source_expr}, '') ILIKE :search OR "
            "COALESCE(r.main_reason, '') ILIKE :search"
            ")"
        )
        params["search"] = f"%{str(search).strip()}%"

    where_sql = "WHERE " + " AND ".join(where_parts) if where_parts else ""

    sort_map = {
        "lead_id": "l.source_id",
        "created_at": created_at_expr,
        "closed_at": closed_at_expr,
        "handled_by": handled_by_expr,
        "status": lead_status_expr,
        "source": lead_source_expr,
        "score": "r.overall_score",
        "overall_score": "r.overall_score",
        "priority": "r.overall_priority_score",
        "overall_priority_score": "r.overall_priority_score",
        "risk": "r.overall_risk",
        "updated_at": "r.updated_at",
        "last_activity_at": lvs_last_activity_expr,
    }
    sort_expr = sort_map.get(str(sort_by or "priority"), "r.overall_priority_score")
    direction = "ASC" if str(sort_dir or "desc").lower() == "asc" else "DESC"
    secondary_order = "l.source_id DESC" if sort_expr != "l.source_id" else "r.updated_at DESC NULLS LAST"

    rows = q(
        db,
        f"""
        SELECT
            l.source_id AS lead_id,
            {created_at_expr} AS created_at,
            {closed_at_expr} AS closed_at,
            {synced_at_expr} AS lead_synced_at,
            {lead_status_expr} AS lead_status,
            {lead_source_expr} AS lead_source,
            {handled_by_expr} AS handled_by,
            {booking_id_expr} AS booking_id,
            {user_id_expr} AS user_id,
            {phone_expr} AS contact_phone,
            {email_expr} AS contact_email,
            {lvs_customer_name_expr} AS customer_name,
            {lvs_property_name_expr} AS property_name,
            {lvs_last_activity_expr} AS last_activity_at,
            {lvs_next_followup_expr} AS next_followup_at,
            COALESCE(r.status, 'unrated') AS review_status,
            r.model,
            r.context_version,
            r.overall_score,
            r.overall_priority_score,
            r.lead_handling_score,
            r.customer_perspective_score,
            r.overall_risk,
            r.post_booking_risk,
            r.main_reason,
            r.action_rows,
            r.stakeholder_scores,
            r.actor_scores,
            r.summary AS review_summary,
            r.updated_at AS rating_updated_at,
            COUNT(*) OVER() AS _total_count
        FROM {lead_table} l
        LEFT JOIN {cache_table} r
          ON r.lead_id = l.source_id
        {lvs_join_sql}
        {where_sql}
        ORDER BY {sort_expr} {direction} NULLS LAST, {secondary_order}
        LIMIT :limit_n OFFSET :offset_n
        """,
        params,
    )

    total = int(rows[0].get("_total_count") or 0) if rows else 0
    dashboard_rows: List[Dict[str, Any]] = []
    for row in rows:
        review_summary = row.get("review_summary") if isinstance(row.get("review_summary"), dict) else {}
        action_rows = row.get("action_rows") if isinstance(row.get("action_rows"), list) else []
        stakeholder_scores = row.get("stakeholder_scores") if isinstance(row.get("stakeholder_scores"), list) else []
        actor_scores = row.get("actor_scores") if isinstance(row.get("actor_scores"), list) else []
        top_action = _dashboard_top_action(action_rows)
        rating = compact_dict({
            "status": row.get("review_status"),
            "model": row.get("model"),
            "context_version": row.get("context_version"),
            "overall_score": row.get("overall_score"),
            "overall_priority_score": row.get("overall_priority_score"),
            "lead_handling_score": row.get("lead_handling_score"),
            # "customer_perspective_score": row.get("customer_perspective_score"),
            "overall_risk": row.get("overall_risk"),
            "post_booking_risk": row.get("post_booking_risk"),
            "main_reason": row.get("main_reason"),
            "updated_at": row.get("rating_updated_at"),
            "needs_recompute": str(row.get("review_status") or "").lower() == "stale",
        })
        dashboard_rows.append(compact_dict({
            "lead_id": row.get("lead_id"),
            "lead": compact_dict({
                "lead_id": row.get("lead_id"),
                "status": row.get("lead_status"),
                "source": row.get("lead_source"),
                "handled_by": row.get("handled_by"),
                "created_at": row.get("created_at"),
                "closed_at": row.get("closed_at"),
                "booking_id": row.get("booking_id"),
                "user_id": row.get("user_id"),
                "contact_phone": row.get("contact_phone"),
                "contact_email": row.get("contact_email"),
                "customer_name": row.get("customer_name"),
                "property_name": row.get("property_name"),
                "last_activity_at": row.get("last_activity_at"),
                "next_followup_at": row.get("next_followup_at"),
            }),
            "rating": rating,
            "stakeholders": compact_dict({
                "scorecard": stakeholder_scores,
            }),
            "actors": compact_dict({
                "scorecard": actor_scores,
            }),
            "counts": compact_dict({
                "events": _dashboard_count(review_summary, "events"),
                "calls": _dashboard_count(review_summary, "calls"),
                "whatsapp": _dashboard_count(review_summary, "whatsapp"),
                "emails": _dashboard_count(review_summary, "emails"),
                "site_visits": _dashboard_count(review_summary, "site_visits"),
                "bookings": _dashboard_count(review_summary, "bookings"),
                "customer_to_business": _dashboard_count(review_summary, "customer_to_business"),
                "business_to_customer": _dashboard_count(review_summary, "business_to_customer"),
            }),
            "top_action": top_action if include_actions else None,
            "action_count": len(action_rows),
            "stakeholder_count": len(stakeholder_scores),
            "actor_count": len(actor_scores),
            "detail_url": (
                "http://127.0.0.1:8000/analytics/capabilities/ui"
                "?endpoint=%2Fcommunication%2Freview-lead"
                f"&lead_id={row.get('lead_id')}"
                "&days=90&limit=10000&print_limit=200&max_text=180"
                "&llm=true&display_mode=evidence&include_prompt=true"
            ),
        }))

    columns = [
        {"key": "lead_id", "label": "Lead ID", "sort": True, "filter": "exact"},
        {"key": "lead.status", "label": "Lead status", "sort": True, "filter": "status"},
        {"key": "lead.source", "label": "Source", "sort": True, "filter": "source"},
        {"key": "lead.handled_by", "label": "Handled by", "sort": True, "filter": "text"},
        {"key": "rating.overall_score", "label": "Overall score", "sort": True, "filter": "range"},
        {"key": "rating.overall_priority_score", "label": "Priority", "sort": True, "filter": "range"},
        {"key": "rating.lead_handling_score", "label": "Lead handling", "sort": True},
        # {"key": "rating.customer_perspective_score", "label": "Customer score", "sort": True},
        {"key": "rating.overall_risk", "label": "Risk", "sort": True, "filter": "risk"},
        {"key": "counts.events", "label": "Events"},
        {"key": "counts.calls", "label": "Calls"},
        {"key": "counts.whatsapp", "label": "WA"},
        {"key": "counts.emails", "label": "Email"},
        {"key": "top_action.action", "label": "Top action"},
        {"key": "rating.updated_at", "label": "Rated at", "sort": True},
    ]

    return compact_dict({
        "view": "lead_dashboard",
        "context_version": LEAD_REVIEW_CONTEXT_VERSION,
        "source_table": LEAD_LLM_REVIEW_CACHE_TABLE,
        "only_rated": only_rated,
        "limit": params["limit_n"],
        "offset": params["offset_n"],
        "total": total,
        "count": len(dashboard_rows),
        "sort": {"by": sort_by, "dir": direction.lower()},
        "filters": compact_dict({
            "lead_id": lead_id,
            "handled_by": handled_by,
            "status": status,
            "source": source,
            "risk": risk,
            "review_status": review_status,
            "min_score": min_score,
            "max_score": max_score,
            "min_priority": min_priority,
            "max_priority": max_priority,
            "search": search,
        }),
        "columns": columns,
        "rows": dashboard_rows,
    })


