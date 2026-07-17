from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .cache import ensure_booking_review_cache_table
from .common import (
    BOOKING_LLM_REVIEW_CACHE_TABLE,
    BOOKING_REVIEW_CONTEXT_VERSION,
    DEFAULT_SCHEMA,
    compact_dict,
    q,
    score_number,
    schema_ident,
    table_columns,
    table_exists,
    table_ref,
)

ACTIVE_TICKET_STATUSES = {"open", "in progress", "in_progress", "reopened","reopen", "pending", "assigned", "new"}


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


def _existing_raw_expr(alias: str, columns: set[str], candidates: List[str], *, default: str = "NULL") -> str:
    parts = [f"{alias}.{_safe_sql_ident(column)}" for column in candidates if column in columns]
    if not parts:
        return default
    if len(parts) == 1:
        return parts[0]
    return f"COALESCE({', '.join(parts)})"

def _dashboard_ops_desk_action(stakeholder_scores: Any) -> Dict[str, Any]:
    scores = stakeholder_scores if isinstance(stakeholder_scores, list) else []
    ops_row = next(
        (
            row for row in scores
            if isinstance(row, dict)
            and str(row.get("stakeholder_team") or row.get("team") or "").strip().lower() == "ops"
        ),
        {},
    )
    subteams = ops_row.get("subteam_scores") if isinstance(ops_row, dict) else []
    subteams = subteams if isinstance(subteams, list) else []
    if not subteams:
        return compact_dict(
            {
                "priority_score": 1,
                "owner_team": "Ops",
                "action": "No immediate Ops action needed",
                "evidence": "No Ops subteam action found",
            }
        )
    allowed_subteams = {"desk", "field", "asset"}
    def priority_value(item: Any) -> float:
        if not isinstance(item, dict):
            return -1
        try:
            return float(item.get("priority_score") or item.get("priority") or 0)
        except Exception:
            return 0
    def has_real_gap(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        gap = str(item.get("gaps") or "").strip().lower()
        return bool(gap) and gap not in {
            "none",
            "no",
            "nil",
            "na",
            "n/a",
            "not visible",
            "no action needed",
            "no immediate action needed",
            "no immediate ops action needed",
        }
    ops_subteams = [
        row for row in subteams
        if isinstance(row, dict)
        and str(row.get("subteam") or row.get("ops_subteam") or row.get("team") or "").strip().lower()
        in allowed_subteams
    ]
    if not ops_subteams:
        return compact_dict(
            {
                "priority_score": 1,
                "owner_team": "Ops",
                "action": "No immediate Ops action needed",
                "evidence": "No Desk/Field/Asset subteam score found",
            }
        )
    actionable_subteams = [row for row in ops_subteams if has_real_gap(row)]
    if not actionable_subteams:
        return compact_dict(
            {
                "priority_score": 1,
                "owner_team": "Ops",
                "action": "No immediate Ops action needed",
                "evidence": "Desk, Field, and Asset have no visible action gap",
            }
        )
    top = max(actionable_subteams, key=priority_value, default={})
    gaps = str(top.get("gaps") or "").strip()
    return compact_dict(
        {
            "priority_score": top.get("priority_score") or 1,
            "owner_team": "Ops / " + str(top.get("subteam") or top.get("ops_subteam") or "Subteam"),
            "action": gaps,
            "evidence": top.get("evidence") or top.get("handled") or "",
        }
    )
   
def _dashboard_top_action(action_rows: Any) -> Dict[str, Any]:
    rows = action_rows if isinstance(action_rows, list) else []
    if not rows:
        return {}

    def priority_value(item: Any) -> float:
        if not isinstance(item, dict):
            return -1
        score = score_number(item.get("priority_score"))
        try:
            return float(score) if score is not None else -1
        except Exception:
            return -1

    top = max((row for row in rows if isinstance(row, dict)), key=priority_value, default={})
    return compact_dict(
        {
            "priority_score": top.get("priority_score"),
            "owner_team": top.get("owner_team") or top.get("owner"),
            "action": top.get("action"),
            "evidence": top.get("evidence"),
        }
    )


def _dashboard_work_action(row: Dict[str, Any], action_rows: Any) -> Dict[str, Any]:
    if row.get("work_action") or row.get("work_owner_team") or row.get("work_priority_score") is not None:
        return compact_dict(
            {
                "priority_score": row.get("work_priority_score"),
                "owner_team": row.get("work_owner_team"),
                "action": row.get("work_action"),
                "evidence": row.get("work_evidence"),
                "is_no_action": row.get("is_no_action"),
            }
        )
    return _dashboard_top_action(action_rows)


def _summary_count(summary: Dict[str, Any], key: str) -> Any:
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    return counts.get(key)


def _ticket_lateral_sql(schema: str, id_exprs: list[str]) -> str:
    table_name = "staging_user_ticket"
    in_values = ", ".join(f"{expr}::text" for expr in id_exprs)
    status_list = ", ".join(f"'{status}'" for status in sorted(ACTIVE_TICKET_STATUSES))
    table = table_ref(schema, table_name)
    return f"""
    LEFT JOIN LATERAL (
        SELECT
            COUNT(*)::int AS total_ticket_count,
            COUNT(*) FILTER (WHERE LOWER(TRIM(COALESCE(t.status::text, ''))) IN ({status_list}))::int AS open_ticket_count,
            COUNT(*) FILTER (WHERE LOWER(TRIM(COALESCE(t.status::text, ''))) NOT IN ({status_list}))::int AS closed_ticket_count,
            MAX(COALESCE(t.close_date, t.created_at, t.synced_at)) AS ticket_last_activity_at
        FROM {table} t
        WHERE t.booking_id::text IN ({in_values})
    ) ticket_stats ON TRUE
    """


def _property_join_sql(db: Session, schema: str, b_cols: set[str]) -> tuple[str, dict[str, str], set[str]]:
    if not table_exists(db, schema, "staging_property_unit") or "prop_id" not in b_cols:
        return "", {}, set()
    pu_cols = table_columns(db, schema, "staging_property_unit")
    if "prop_id" not in pu_cols and "source_id" not in pu_cols:
        return "", {}, pu_cols
    prop_key = "prop_id" if "prop_id" in pu_cols else "source_id"
    order_col = "last_updated_on" if "last_updated_on" in pu_cols else "synced_at" if "synced_at" in pu_cols else prop_key
    select_parts = [f"pu_raw.{prop_key}::text AS prop_key"]
    for column in ("prop_id", "unit_name", "display_property_name", "listing_title", "building_id", "rms_prop"):
        if column in pu_cols:
            select_parts.append(f"pu_raw.{column}")
        else:
            select_parts.append(f"NULL AS {column}")
    join_sql = f"""
    LEFT JOIN (
        SELECT *
        FROM (
            SELECT
                {', '.join(select_parts)},
                ROW_NUMBER() OVER (PARTITION BY pu_raw.{prop_key}::text ORDER BY pu_raw.{order_col} DESC NULLS LAST) AS _rn
            FROM {table_ref(schema, 'staging_property_unit')} pu_raw
        ) pu_ranked
        WHERE _rn = 1
    ) pu ON pu.prop_key = b.prop_id::text
    """
    exprs = {
        "property_name": "COALESCE(NULLIF(TRIM(pu.unit_name::text), ''), NULLIF(TRIM(pu.display_property_name::text), ''), NULLIF(TRIM(pu.listing_title::text), ''))",
        "building_id": "pu.building_id",
        "rms_prop": "pu.rms_prop",
    }
    return join_sql, exprs, pu_cols


def _building_join_sql(db: Session, schema: str, building_expr: str) -> tuple[str, dict[str, str], set[str]]:
    if not table_exists(db, schema, "staging_buildings") or building_expr == "NULL":
        return "", {}, set()
    bd_cols = table_columns(db, schema, "staging_buildings")
    if "building_id" not in bd_cols and "source_id" not in bd_cols:
        return "", {}, bd_cols
    key = "building_id" if "building_id" in bd_cols else "source_id"
    select_parts = [f"bd_raw.{key}::text AS building_key"]
    for column in ("building_id", "building_name", "caretaker", "supervisor", "ops_manager", "finance_supervisor", "sales", "marketing"):
        if column in bd_cols:
            select_parts.append(f"bd_raw.{column}")
        else:
            select_parts.append(f"NULL AS {column}")
    order_col = "updated_on" if "updated_on" in bd_cols else "synced_at" if "synced_at" in bd_cols else key
    join_sql = f"""
    LEFT JOIN (
        SELECT *
        FROM (
            SELECT
                {', '.join(select_parts)},
                ROW_NUMBER() OVER (PARTITION BY bd_raw.{key}::text ORDER BY bd_raw.{order_col} DESC NULLS LAST) AS _rn
            FROM {table_ref(schema, 'staging_buildings')} bd_raw
        ) bd_ranked
        WHERE _rn = 1
    ) bd ON bd.building_key = {building_expr}::text
    """
    exprs = {
        "building_name": "NULLIF(TRIM(bd.building_name::text), '')",
        "sales_owner": "NULLIF(TRIM(bd.sales::text), '')",
        "caretaker": "NULLIF(TRIM(bd.caretaker::text), '')",
        "ops_owner": "COALESCE(NULLIF(TRIM(bd.ops_manager::text), ''), NULLIF(TRIM(bd.supervisor::text), ''))",
        "finance_owner": "NULLIF(TRIM(bd.finance_supervisor::text), '')",
        "marketing_owner": "NULLIF(TRIM(bd.marketing::text), '')",
    }
    return join_sql, exprs, bd_cols




def _can_use_fast_cache_dashboard(
    *,
    booking_id: Optional[int],
    status: Optional[str],
    current_state: Optional[str],
    property_name: Optional[str],
    building_id: Optional[int],
    owner: Optional[str],
    stay_from: Optional[Any],
    stay_to: Optional[Any],
    search: Optional[str],
    only_rated: bool,
    sort_by: str,
) -> bool:
    """True when the request can be served from booking_communication_review only.

    New reviews/backfills hydrate booking/property/building/owner scope into the
    cache row, so the fast path can also handle common dashboard filters. The
    normal path remains available for unrated-booking views.
    """
    if not only_rated:
        return False
    return str(sort_by or "priority") in {
        "booking_id",
        "status",
        "current_state",
        "property_name",
        "building_id",
        "score",
        "overall_score",
        "priority",
        "work_priority_score",
        "overall_priority_score",
        "ops_score",
        "sales_score",
        "finance_score",
        "caretaker_score",
        "desk_score",
        "field_score",
        "asset_score",
        "updated_at",
        "rated_at",
        "travel_from_date",
        "travel_to_date",
    }


def _list_booking_dashboard_rows_fast_cache(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    booking_id: Optional[int] = None,
    status: Optional[str] = None,
    current_state: Optional[str] = None,
    property_name: Optional[str] = None,
    building_id: Optional[int] = None,
    owner: Optional[str] = None,
    risk: Optional[str] = None,
    review_status: Optional[str] = None,
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
    min_priority: Optional[float] = None,
    max_priority: Optional[float] = None,
    stay_from: Optional[Any] = None,
    stay_to: Optional[Any] = None,
    search: Optional[str] = None,
    include_actions: bool = True,
    include_stakeholders: bool = True,
    sort_by: str = "priority",
    sort_dir: str = "desc",
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    """Ultra-fast rated dashboard path using the hydrated booking review cache."""
    table = table_ref(schema, BOOKING_LLM_REVIEW_CACHE_TABLE)
    params: Dict[str, Any] = {
        "limit_n": max(1, min(int(limit or 100), 1000)),
        "offset_n": max(0, int(offset or 0)),
    }
    priority_expr = "COALESCE(r.work_priority_score, r.overall_priority_score)"
    where_parts: List[str] = ["r.booking_id IS NOT NULL"]
    if booking_id is not None:
        where_parts.append("r.booking_id = :booking_id")
        params["booking_id"] = int(booking_id)
    if status:
        where_parts.append("LOWER(COALESCE(r.booking_status, '')) = :booking_status")
        params["booking_status"] = str(status).strip().lower()
    if current_state:
        where_parts.append("LOWER(COALESCE(r.stay_state, '')) = :current_state")
        params["current_state"] = str(current_state).strip().lower()
    if property_name:
        where_parts.append("COALESCE(r.property_name, '') ILIKE :property_name")
        params["property_name"] = f"%{str(property_name).strip()}%"
    if building_id is not None:
        where_parts.append("r.building_id = :building_id")
        params["building_id"] = int(building_id)
    if owner:
        where_parts.append(
            "LOWER(COALESCE(r.sales_owner, '') || ' ' || COALESCE(r.caretaker, '') || ' ' || "
            "COALESCE(r.ops_owner, '') || ' ' || COALESCE(r.ops_manager, '') || ' ' || "
            "COALESCE(r.finance_owner, '') || ' ' || COALESCE(r.finance_manager, '')) LIKE :owner"
        )
        params["owner"] = f"%{str(owner).strip().lower()}%"
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
        where_parts.append(f"{priority_expr} >= :min_priority")
        params["min_priority"] = float(min_priority)
    if max_priority is not None:
        where_parts.append(f"{priority_expr} <= :max_priority")
        params["max_priority"] = float(max_priority)
    if stay_from:
        where_parts.append("r.travel_to_date >= CAST(:stay_from AS date)")
        params["stay_from"] = str(stay_from)[:10]
    if stay_to:
        where_parts.append("r.travel_from_date <= CAST(:stay_to AS date)")
        params["stay_to"] = str(stay_to)[:10]
    if search:
        where_parts.append(
            "(r.booking_id::text ILIKE :search OR COALESCE(r.property_name, '') ILIKE :search OR "
            "COALESCE(r.building_name, '') ILIKE :search OR COALESCE(r.work_action, '') ILIKE :search OR "
            "COALESCE(r.work_evidence, '') ILIKE :search OR COALESCE(r.main_reason, '') ILIKE :search OR "
            "COALESCE(r.sales_owner, '') ILIKE :search OR COALESCE(r.caretaker, '') ILIKE :search OR "
            "COALESCE(r.ops_owner, '') ILIKE :search OR COALESCE(r.finance_owner, '') ILIKE :search)"
        )
        params["search"] = f"%{str(search).strip()}%"

    sort_map = {
        "booking_id": "r.booking_id",
        "status": "r.booking_status",
        "current_state": "r.stay_state",
        "property_name": "r.property_name",
        "building_id": "r.building_id",
        "score": "r.overall_score",
        "overall_score": "r.overall_score",
        "priority": priority_expr,
        "work_priority_score": priority_expr,
        "overall_priority_score": "r.overall_priority_score",
        "updated_at": "r.updated_at",
        "rated_at": "r.updated_at",
        "travel_from_date": "r.travel_from_date",
        "travel_to_date": "r.travel_to_date",
        "ops_score": """
        (
        SELECT NULLIF(s->>'score', '')::numeric
        FROM jsonb_array_elements(r.stakeholder_scores) s
        WHERE LOWER(TRIM(COALESCE(s->>'stakeholder_team', s->>'team', ''))) = 'ops'
        LIMIT 1
        )
        """,

        "sales_score": """
        (
        SELECT NULLIF(s->>'score', '')::numeric
        FROM jsonb_array_elements(r.stakeholder_scores) s
        WHERE LOWER(TRIM(COALESCE(s->>'stakeholder_team', s->>'team', ''))) = 'sales'
        LIMIT 1
        )
        """,

        "finance_score": """
        (
        SELECT NULLIF(s->>'score', '')::numeric
        FROM jsonb_array_elements(r.stakeholder_scores) s
        WHERE LOWER(TRIM(COALESCE(s->>'stakeholder_team', s->>'team', ''))) = 'finance'
        LIMIT 1
        )
        """,

        "caretaker_score": """
        (
        SELECT NULLIF(s->>'score', '')::numeric
        FROM jsonb_array_elements(r.stakeholder_scores) s
        WHERE LOWER(TRIM(COALESCE(s->>'stakeholder_team', s->>'team', ''))) = 'caretaker'
        LIMIT 1
        )
        """,

        "desk_score": """
        (
        SELECT NULLIF(sub->>'score', '')::numeric
        FROM jsonb_array_elements(r.stakeholder_scores) s
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(s->'subteam_scores', '[]'::jsonb)) sub
        WHERE LOWER(TRIM(COALESCE(s->>'stakeholder_team', s->>'team', ''))) = 'ops'
            AND LOWER(TRIM(COALESCE(sub->>'subteam', sub->>'ops_subteam', sub->>'team', ''))) = 'desk'
        LIMIT 1
        )
        """,

        "field_score": """
        (
        SELECT NULLIF(sub->>'score', '')::numeric
        FROM jsonb_array_elements(r.stakeholder_scores) s
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(s->'subteam_scores', '[]'::jsonb)) sub
        WHERE LOWER(TRIM(COALESCE(s->>'stakeholder_team', s->>'team', ''))) = 'ops'
            AND LOWER(TRIM(COALESCE(sub->>'subteam', sub->>'ops_subteam', sub->>'team', ''))) = 'field'
        LIMIT 1
        )
        """,

        "asset_score": """
        (
        SELECT NULLIF(sub->>'score', '')::numeric
        FROM jsonb_array_elements(r.stakeholder_scores) s
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(s->'subteam_scores', '[]'::jsonb)) sub
        WHERE LOWER(TRIM(COALESCE(s->>'stakeholder_team', s->>'team', ''))) = 'ops'
            AND LOWER(TRIM(COALESCE(sub->>'subteam', sub->>'ops_subteam', sub->>'team', ''))) = 'asset'
        LIMIT 1
        )
        """,
    }
    sort_expr = sort_map.get(str(sort_by or "priority"), priority_expr)
    direction = "ASC" if str(sort_dir or "desc").lower() == "asc" else "DESC"
    secondary_order = "r.booking_id DESC" if sort_expr != "r.booking_id" else "r.updated_at DESC NULLS LAST"
    where_sql = "WHERE " + " AND ".join(where_parts)
    action_select = "r.action_rows" if include_actions else "'[]'::jsonb"
    stakeholder_select = "r.stakeholder_scores" if include_stakeholders else "'[]'::jsonb"

    rows = q(
        db,
        f"""
        SELECT
            r.booking_id,
            COALESCE(r.status, 'unrated') AS review_status,
            r.model,
            r.context_version,
            r.context_hash,
            r.overall_score,
            r.overall_priority_score,
            r.work_priority_score,
            r.customer_perspective_score,
            r.operations_score,
            r.support_score,
            r.main_reason,
            r.work_owner_team,
            r.work_action,
            r.work_evidence,
            r.is_no_action,
            r.action_owner_teams,
            r.issue_themes,
            r.booking_status,
            r.stay_state,
            r.booking_type,
            r.property_id,
            r.property_name,
            r.building_id,
            r.building_name,
            r.travel_from_date,
            r.travel_to_date,
            r.sales_owner,
            r.ops_owner,
            r.ops_manager,
            r.caretaker,
            r.finance_owner,
            r.finance_manager,
            r.support_owner,
            {action_select} AS action_rows,
            {stakeholder_select} AS stakeholder_scores,
            '[]'::jsonb AS actor_scores,
            COALESCE(r.summary, '{{}}'::jsonb) AS review_summary,
            r.stale_at,
            r.updated_at AS rating_updated_at,
            COUNT(*) OVER() AS _total_count
        FROM {table} r
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
        rating = compact_dict(
            {
                "status": row.get("review_status"),
                "model": row.get("model"),
                "context_version": row.get("context_version"),
                "context_hash": row.get("context_hash"),
                "overall_score": row.get("overall_score"),
                "overall_priority_score": row.get("overall_priority_score"),
                "work_priority_score": row.get("work_priority_score"),
                "customer_perspective_score": row.get("customer_perspective_score"),
                "operations_score": row.get("operations_score"),
                "support_score": row.get("support_score"),
                "main_reason": row.get("main_reason"),
                "is_no_action": row.get("is_no_action"),
                "action_owner_teams": row.get("action_owner_teams"),
                "issue_themes": row.get("issue_themes"),
                "updated_at": row.get("rating_updated_at"),
                "stale_at": row.get("stale_at"),
                "needs_recompute": str(row.get("review_status") or "").lower() == "stale" or bool(row.get("stale_at")),
            }
        )
        dashboard_rows.append(
            compact_dict(
                {
                    "booking_id": row.get("booking_id"),
                    "booking": compact_dict(
                        {
                            "booking_id": row.get("booking_id"),
                            "status": row.get("booking_status"),
                            "type": row.get("booking_type"),
                            "current_state": row.get("stay_state"),
                            "prop_id": row.get("property_id"),
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
                            "caretaker": row.get("caretaker"),
                            "ops_owner": row.get("ops_owner"),
                            "ops_manager": row.get("ops_manager"),
                            "finance_owner": row.get("finance_owner"),
                            "finance_manager": row.get("finance_manager"),
                            "support_owner": row.get("support_owner"),
                            "scorecard": stakeholder_scores if include_stakeholders else None,
                        }
                    ),
                    "rating": rating,
                    "counts": compact_dict(
                        {
                            "events": _summary_count(review_summary, "events") or _summary_count(review_summary, "messages"),
                            "calls": _summary_count(review_summary, "calls"),
                            "whatsapp": _summary_count(review_summary, "whatsapp"),
                            "emails": _summary_count(review_summary, "emails"),
                            "tickets_total": _summary_count(review_summary, "tickets_total"),
                            "open_tickets": _summary_count(review_summary, "open_tickets"),
                            "ops_open_tickets": _summary_count(review_summary, "ops_open_tickets"),
                            "closed_tickets": _summary_count(review_summary, "closed_tickets"),
                        }
                    ),
                    "top_action": _dashboard_work_action(row, action_rows) if include_actions else None,
                    "ops_desk_top_action": _dashboard_ops_desk_action(stakeholder_scores) if include_actions else None,
                    "action_count": len(action_rows),
                    "stakeholder_count": len(stakeholder_scores),
                    "actor_count": 0,
                    "detail_url": (
                        "http://127.0.0.1:8000/analytics/capabilities/ui"
                        "?endpoint=%2Fanalytics%2Fcapabilities%2Fbookings%2Freview"
                        f"&booking_id={row.get('booking_id')}"
                        "&customer_days=30&max_llm_messages=12&max_llm_text_chars=220"
                        "&llm=true&include_prompt=true"
                    ),
                }
            )
        )
    def _stakeholder_score(item: dict[str, Any], team: str) -> float:
        scores = item.get("stakeholders", {}).get("scorecard") or item.get("stakeholder_scores") or []
        for s in scores:
            if str(s.get("stakeholder_team") or s.get("team") or "").strip().lower() == team.lower():
                try:
                    return float(s.get("score") or 0)
                except Exception:
                    return 0
        return 0

    def _ops_subteam_score(item: dict[str, Any], subteam: str) -> float:
        scores = item.get("stakeholders", {}).get("scorecard") or item.get("stakeholder_scores") or []
        ops_row = next(
            (
                s for s in scores
                if str(s.get("stakeholder_team") or s.get("team") or "").strip().lower() == "ops"
            ),
            {},
        )
        for row in ops_row.get("subteam_scores") or []:
            if str(row.get("subteam") or row.get("ops_subteam") or row.get("team") or "").strip().lower() == subteam.lower():
                try:
                    return float(row.get("score") or 0)
                except Exception:
                    return 0
        return 0

    custom_sort_map = {
        "ops_score": lambda item: _stakeholder_score(item, "Ops"),
        "sales_score": lambda item: _stakeholder_score(item, "Sales"),
        "finance_score": lambda item: _stakeholder_score(item, "Finance"),
        "caretaker_score": lambda item: _stakeholder_score(item, "Caretaker"),
        "desk_score": lambda item: _ops_subteam_score(item, "Desk"),
        "field_score": lambda item: _ops_subteam_score(item, "Field"),
        "asset_score": lambda item: _ops_subteam_score(item, "Asset"),
    }
    if str(sort_by or "") in custom_sort_map:
        dashboard_rows.sort(
            key=custom_sort_map[str(sort_by)],
            reverse=str(sort_dir or "desc").lower() == "desc",
        )
    columns = [
        {"key": "booking_id", "label": "Booking ID", "sort": True, "filter": "exact"},
        {"key": "booking.status", "label": "Booking status", "sort": True, "filter": "status"},
        {"key": "booking.current_state", "label": "Current state", "sort": True, "filter": "status"},
        {"key": "booking.property_name", "label": "Property", "sort": True, "filter": "text"},
        {"key": "booking.building_id", "label": "Building", "sort": True, "filter": "exact"},
        {"key": "stakeholders.caretaker", "label": "Caretaker"},
        {"key": "stakeholders.ops_manager", "label": "Ops manager"},
        {"key": "stakeholders.finance_manager", "label": "Finance manager"},
        {"key": "rating.overall_score", "label": "Overall score", "sort": True, "filter": "range"},
        {"key": "rating.work_priority_score", "label": "Work priority", "sort": True, "filter": "range"},
        {"key": "rating.overall_priority_score", "label": "LLM priority", "sort": True, "filter": "range"},
        {"key": "rating.customer_perspective_score", "label": "Customer score", "sort": True},
        {"key": "rating.operations_score", "label": "Ops score", "sort": True},
        {"key": "rating.support_score", "label": "Support score", "sort": True},
        {"key": "top_action.action", "label": "Top action"},
        {"key": "rating.updated_at", "label": "Rated at", "sort": True},
    ]
    return compact_dict(
        {
            "view": "booking_dashboard",
            "fast_path": "booking_review_cache_hydrated",
            "context_version": BOOKING_REVIEW_CONTEXT_VERSION,
            "source_table": BOOKING_LLM_REVIEW_CACHE_TABLE,
            "only_rated": True,
            "limit": params["limit_n"],
            "offset": params["offset_n"],
            "total": total,
            "count": len(dashboard_rows),
            "sort": {"by": sort_by, "dir": direction.lower()},
            "filters": compact_dict(
                {
                    "booking_id": booking_id,
                    "status": status,
                    "current_state": current_state,
                    "property_name": property_name,
                    "building_id": building_id,
                    "owner": owner,
                    "review_status": review_status,
                    "min_score": min_score,
                    "max_score": max_score,
                    "min_priority": min_priority,
                    "max_priority": max_priority,
                    "stay_from": stay_from,
                    "stay_to": stay_to,
                    "search": search,
                }
            ),
            "columns": columns,
            "rows": dashboard_rows,
        }
    )

def list_booking_dashboard_rows(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    booking_id: Optional[int] = None,
    status: Optional[str] = None,
    current_state: Optional[str] = None,
    property_name: Optional[str] = None,
    building_id: Optional[int] = None,
    owner: Optional[str] = None,
    risk: Optional[str] = None,
    review_status: Optional[str] = None,
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
    min_priority: Optional[float] = None,
    max_priority: Optional[float] = None,
    stay_from: Optional[Any] = None,
    stay_to: Optional[Any] = None,
    search: Optional[str] = None,
    only_rated: bool = True,
    include_actions: bool = True,
    include_stakeholders: bool = True,
    sort_by: str = "priority",
    sort_dir: str = "desc",
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    """Return compact booking dashboard rows.

    One row per booking, basic booking fields plus cached LLM rating. This does
    not call the LLM. Ratings are read from booking_communication_review and can
    be refreshed by /bookings/review/llm-rating or a stale recompute job.
    """
    # For dashboard reads, avoid running DDL/index/migration checks when the
    # cache table already exists. Those checks can take locks and dominate a
    # read-only API request. Use the ensure path only for first-time setup.
    if not table_exists(db, schema, BOOKING_LLM_REVIEW_CACHE_TABLE):
        ensure_booking_review_cache_table(db, schema)
    else:
        cache_columns = table_columns(db, schema, BOOKING_LLM_REVIEW_CACHE_TABLE)
        if "work_priority_score" not in cache_columns or "property_name" not in cache_columns:
            ensure_booking_review_cache_table(db, schema, force=True)

    if _can_use_fast_cache_dashboard(
        booking_id=booking_id,
        status=status,
        current_state=current_state,
        property_name=property_name,
        building_id=building_id,
        owner=owner,
        stay_from=stay_from,
        stay_to=stay_to,
        search=search,
        only_rated=only_rated,
        sort_by=sort_by,
    ):
        return _list_booking_dashboard_rows_fast_cache(
            db=db,
            schema=schema,
            booking_id=booking_id,
            status=status,
            current_state=current_state,
            property_name=property_name,
            building_id=building_id,
            owner=owner,
            risk=risk,
            review_status=review_status,
            min_score=min_score,
            max_score=max_score,
            min_priority=min_priority,
            max_priority=max_priority,
            stay_from=stay_from,
            stay_to=stay_to,
            search=search,
            include_actions=include_actions,
            include_stakeholders=include_stakeholders,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
    if not table_exists(db, schema, "staging_booking_confirm"):
        raise ValueError("staging_booking_confirm table is required for booking dashboard")

    b_cols = table_columns(db, schema, "staging_booking_confirm")
    if "source_id" not in b_cols and "booking_id" not in b_cols:
        raise ValueError("staging_booking_confirm.source_id or booking_id is required for booking dashboard")

    booking_table = table_ref(schema, "staging_booking_confirm")
    cache_table = table_ref(schema, BOOKING_LLM_REVIEW_CACHE_TABLE)
    id_expr = "b.source_id" if "source_id" in b_cols else "b.booking_id"
    alt_id_expr = "b.booking_id" if "booking_id" in b_cols else "b.source_id" if "source_id" in b_cols else id_expr
    id_exprs = []
    for expr in (id_expr, alt_id_expr):
        if expr not in id_exprs:
            id_exprs.append(expr)

    status_expr = _existing_text_expr("b", b_cols, ["booking_status", "status"])
    booking_type_expr = _existing_text_expr("b", b_cols, ["booking_type", "period", "type"])
    customer_name_expr = _existing_text_expr("b", b_cols, ["traveller_name", "customer_name", "name", "guest_name"])
    customer_phone_expr = _existing_text_expr("b", b_cols, ["traveller_contact_num", "contact_number", "phone", "mobile", "mobile_number"])
    customer_email_expr = _existing_text_expr("b", b_cols, ["contact_email", "email", "user_email"])
    user_id_expr = _existing_text_expr("b", b_cols, ["user_id"])
    lead_id_expr = _existing_text_expr("b", b_cols, ["lead_id"])
    prop_id_expr = _existing_text_expr("b", b_cols, ["prop_id"])
    booking_datetime_expr = _existing_raw_expr("b", b_cols, ["booking_datetime", "created_at", "synced_at"])
    travel_from_expr = _existing_raw_expr("b", b_cols, ["travel_from_date"])
    travel_to_expr = _existing_raw_expr("b", b_cols, ["travel_to_date"])
    synced_at_expr = _existing_raw_expr("b", b_cols, ["synced_at"])
    booking_sales_expr = _existing_text_expr("b", b_cols, ["created_by", "salesperson", "sales", "added_by"])

    property_join, pu_exprs, _pu_cols = _property_join_sql(db, schema, b_cols)
    property_name_expr = pu_exprs.get("property_name") or _existing_text_expr("b", b_cols, ["property_name", "prop_name", "unit_name"], default="NULL")
    property_building_expr = pu_exprs.get("building_id") or _existing_raw_expr("b", b_cols, ["building_id"], default="NULL")
    if property_building_expr == "NULL" and "building_id" in b_cols:
        property_building_expr = "b.building_id"

    building_join, bd_exprs, _bd_cols = _building_join_sql(db, schema, property_building_expr)
    building_name_expr = bd_exprs.get("building_name") or "NULL"
    sales_owner_expr = f"COALESCE({booking_sales_expr}, {bd_exprs.get('sales_owner') or 'NULL'})"
    caretaker_expr = bd_exprs.get("caretaker") or "NULL"
    ops_owner_expr = bd_exprs.get("ops_owner") or "NULL"
    finance_owner_expr = bd_exprs.get("finance_owner") or "NULL"
    owner_search_expr = f"COALESCE({sales_owner_expr}, '') || ' ' || COALESCE({caretaker_expr}, '') || ' ' || COALESCE({ops_owner_expr}, '') || ' ' || COALESCE({finance_owner_expr}, '')"

    if table_exists(db, schema, "staging_user_ticket"):
        ticket_join = _ticket_lateral_sql(schema, id_exprs)
        total_ticket_expr = "ticket_stats.total_ticket_count"
        open_ticket_expr = "ticket_stats.open_ticket_count"
        closed_ticket_expr = "ticket_stats.closed_ticket_count"
        ticket_activity_expr = "ticket_stats.ticket_last_activity_at"
    else:
        ticket_join = ""
        total_ticket_expr = "NULL"
        open_ticket_expr = "NULL"
        closed_ticket_expr = "NULL"
        ticket_activity_expr = "NULL"

    if travel_from_expr != "NULL" and travel_to_expr != "NULL":
        current_state_expr = f"""
        CASE
            WHEN LOWER(TRIM(COALESCE({status_expr}, ''))) NOT IN ('success', 'booked') THEN 'inactive_non_success'
            WHEN {travel_to_expr}::date < (NOW() AT TIME ZONE 'Asia/Kolkata')::date THEN 'stay_completed'
            WHEN {travel_to_expr}::date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date THEN 'checkout_due'
            WHEN {travel_from_expr}::date <= (NOW() AT TIME ZONE 'Asia/Kolkata')::date
             AND {travel_to_expr}::date >= (NOW() AT TIME ZONE 'Asia/Kolkata')::date THEN 'active_stay'
            WHEN {travel_from_expr}::date > (NOW() AT TIME ZONE 'Asia/Kolkata')::date THEN 'upcoming_active_booking'
            ELSE 'active_booking_open'
        END
        """
    else:
        current_state_expr = "'unknown'"

    cache_join_conditions = " OR ".join(f"r_raw.booking_id::text = {expr}::text" for expr in id_exprs)
    cache_match_rank = "CASE " + " ".join(f"WHEN r_raw.booking_id::text = {expr}::text THEN {idx}" for idx, expr in enumerate(id_exprs)) + " ELSE 99 END"
    cache_join = f"""
    LEFT JOIN LATERAL (
        SELECT *
        FROM {cache_table} r_raw
        WHERE {cache_join_conditions}
        ORDER BY {cache_match_rank}, r_raw.updated_at DESC NULLS LAST
        LIMIT 1
    ) r ON TRUE
    """

    where_parts: List[str] = []
    params: Dict[str, Any] = {
        "limit_n": max(1, min(int(limit or 100), 1000)),
        "offset_n": max(0, int(offset or 0)),
    }
    if only_rated:
        where_parts.append("r.booking_id IS NOT NULL")
    if booking_id is not None:
        where_parts.append("(" + " OR ".join(f"{expr}::text = :booking_id_text" for expr in id_exprs) + ")")
        params["booking_id_text"] = str(int(booking_id))
    if status:
        where_parts.append(f"LOWER({status_expr}) = :booking_status")
        params["booking_status"] = str(status).strip().lower()
    if current_state:
        where_parts.append(f"LOWER(({current_state_expr})::text) = :current_state")
        params["current_state"] = str(current_state).strip().lower()
    if property_name:
        where_parts.append(f"COALESCE({property_name_expr}, '') ILIKE :property_name")
        params["property_name"] = f"%{str(property_name).strip()}%"
    if building_id is not None:
        where_parts.append(f"{property_building_expr}::text = :building_id_text")
        params["building_id_text"] = str(int(building_id))
    if owner:
        where_parts.append(f"LOWER({owner_search_expr}) LIKE :owner")
        params["owner"] = f"%{str(owner).strip().lower()}%"
    if review_status:
        where_parts.append("LOWER(COALESCE(r.status, 'unrated')) = :review_status")
        params["review_status"] = str(review_status).strip().lower()
    if min_score is not None:
        where_parts.append("r.overall_score >= :min_score")
        params["min_score"] = float(min_score)
    if max_score is not None:
        where_parts.append("r.overall_score <= :max_score")
        params["max_score"] = float(max_score)
    priority_expr = "COALESCE(r.work_priority_score, r.overall_priority_score)"
    if min_priority is not None:
        where_parts.append(f"{priority_expr} >= :min_priority")
        params["min_priority"] = float(min_priority)
    if max_priority is not None:
        where_parts.append(f"{priority_expr} <= :max_priority")
        params["max_priority"] = float(max_priority)
    if stay_from and travel_to_expr != "NULL":
        where_parts.append(f"{travel_to_expr}::date >= CAST(:stay_from AS date)")
        params["stay_from"] = str(stay_from)[:10]
    if stay_to and travel_from_expr != "NULL":
        where_parts.append(f"{travel_from_expr}::date <= CAST(:stay_to AS date)")
        params["stay_to"] = str(stay_to)[:10]
    if search:
        where_parts.append(
            "("
            + " OR ".join(
                [
                    f"{id_expr}::text ILIKE :search",
                    f"COALESCE({alt_id_expr}::text, '') ILIKE :search",
                    f"COALESCE({customer_name_expr}, '') ILIKE :search",
                    f"COALESCE({customer_phone_expr}, '') ILIKE :search",
                    f"COALESCE({customer_email_expr}, '') ILIKE :search",
                    f"COALESCE({property_name_expr}, '') ILIKE :search",
                    f"COALESCE({building_name_expr}, '') ILIKE :search",
                    f"COALESCE({owner_search_expr}, '') ILIKE :search",
                    "COALESCE(r.main_reason, '') ILIKE :search",
                ]
            )
            + ")"
        )
        params["search"] = f"%{str(search).strip()}%"

    where_sql = "WHERE " + " AND ".join(where_parts) if where_parts else ""
    sort_map = {
        "booking_id": id_expr,
        "status": status_expr,
        "current_state": "current_state",
        "property_name": "property_name",
        "building_id": "building_id",
        "score": "r.overall_score",
        "overall_score": "r.overall_score",
        "priority": priority_expr,
        "work_priority_score": priority_expr,
        "overall_priority_score": "r.overall_priority_score",
        "updated_at": "r.updated_at",
        "rated_at": "r.updated_at",
        "travel_from_date": "travel_from_date",
        "travel_to_date": "travel_to_date",
        "booking_datetime": "booking_datetime",
        "open_tickets": "open_ticket_count",
        "recent_activity": "recent_activity_count",
    }
    sort_expr = sort_map.get(str(sort_by or "priority"), priority_expr)
    direction = "ASC" if str(sort_dir or "desc").lower() == "asc" else "DESC"
    secondary_order = f"{id_expr} DESC" if sort_expr != id_expr else "r.updated_at DESC NULLS LAST"

    rows = q(
        db,
        f"""
        SELECT
            {id_expr}::bigint AS booking_id,
            {alt_id_expr} AS alternate_booking_id,
            {user_id_expr} AS user_id,
            {lead_id_expr} AS lead_id,
            {prop_id_expr} AS prop_id,
            {status_expr} AS booking_status,
            {booking_type_expr} AS booking_type,
            {customer_name_expr} AS customer_name,
            {customer_phone_expr} AS customer_phone,
            {customer_email_expr} AS customer_email,
            {property_name_expr} AS property_name,
            {property_building_expr} AS building_id,
            {building_name_expr} AS building_name,
            {sales_owner_expr} AS sales_owner,
            {caretaker_expr} AS caretaker,
            {ops_owner_expr} AS ops_owner,
            {finance_owner_expr} AS finance_owner,
            {booking_datetime_expr} AS booking_datetime,
            {travel_from_expr} AS travel_from_date,
            {travel_to_expr} AS travel_to_date,
            {synced_at_expr} AS booking_synced_at,
            ({current_state_expr}) AS current_state,
            {total_ticket_expr} AS total_ticket_count,
            {open_ticket_expr} AS open_ticket_count,
            {closed_ticket_expr} AS closed_ticket_count,
            {ticket_activity_expr} AS ticket_last_activity_at,
            COALESCE((r.summary->'counts'->>'events')::int, (r.summary->'counts'->>'messages')::int, 0) AS recent_activity_count,
            COALESCE(r.status, 'unrated') AS review_status,
            r.model,
            r.context_version,
            r.context_hash,
            r.overall_score,
            r.overall_priority_score,
            r.work_priority_score,
            r.work_owner_team,
            r.work_action,
            r.work_evidence,
            r.is_no_action,
            r.action_owner_teams,
            r.issue_themes,
            r.customer_perspective_score,
            r.operations_score,
            r.support_score,
            r.main_reason,
            r.action_rows,
            r.stakeholder_scores,
            r.actor_scores,
            r.summary AS review_summary,
            r.stale_at,
            r.updated_at AS rating_updated_at,
            COUNT(*) OVER() AS _total_count
        FROM {booking_table} b
        {property_join}
        {building_join}
        {ticket_join}
        {cache_join}
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
        rating = compact_dict(
            {
                "status": row.get("review_status"),
                "model": row.get("model"),
                "context_version": row.get("context_version"),
                "context_hash": row.get("context_hash"),
                "overall_score": row.get("overall_score"),
                "overall_priority_score": row.get("overall_priority_score"),
                "work_priority_score": row.get("work_priority_score"),
                "is_no_action": row.get("is_no_action"),
                "action_owner_teams": row.get("action_owner_teams"),
                "issue_themes": row.get("issue_themes"),
                "customer_perspective_score": row.get("customer_perspective_score"),
                "operations_score": row.get("operations_score"),
                "support_score": row.get("support_score"),
                "main_reason": row.get("main_reason"),
                "updated_at": row.get("rating_updated_at"),
                "stale_at": row.get("stale_at"),
                "needs_recompute": str(row.get("review_status") or "").lower() == "stale" or bool(row.get("stale_at")),
            }
        )
        dashboard_rows.append(
            compact_dict(
                {
                    "booking_id": row.get("booking_id"),
                    "booking": compact_dict(
                        {
                            "booking_id": row.get("booking_id"),
                            "alternate_booking_id": row.get("alternate_booking_id"),
                            "status": row.get("booking_status"),
                            "type": row.get("booking_type"),
                            "current_state": row.get("current_state"),
                            "customer_name": row.get("customer_name"),
                            "customer_phone": row.get("customer_phone"),
                            "customer_email": row.get("customer_email"),
                            "user_id": row.get("user_id"),
                            "lead_id": row.get("lead_id"),
                            "prop_id": row.get("prop_id"),
                            "property_name": row.get("property_name"),
                            "building_id": row.get("building_id"),
                            "building_name": row.get("building_name"),
                            "travel_from_date": row.get("travel_from_date"),
                            "travel_to_date": row.get("travel_to_date"),
                            "booking_datetime": row.get("booking_datetime"),
                        }
                    ),
                    "stakeholders": compact_dict(
                        {
                            "sales_owner": row.get("sales_owner"),
                            "caretaker": row.get("caretaker"),
                            "ops_owner": row.get("ops_owner"),
                            "finance_owner": row.get("finance_owner"),
                            "scorecard": stakeholder_scores if include_stakeholders else None,
                        }
                    ),
                    "rating": rating,
                    "counts": compact_dict(
                        {
                            "events": _summary_count(review_summary, "events") or row.get("recent_activity_count"),
                            "calls": _summary_count(review_summary, "calls"),
                            "whatsapp": _summary_count(review_summary, "whatsapp"),
                            "emails": _summary_count(review_summary, "emails"),
                            "tickets_total": row.get("total_ticket_count"),
                            "open_tickets": row.get("open_ticket_count"),
                            "closed_tickets": row.get("closed_ticket_count"),
                        }
                    ),
                    "top_action": _dashboard_work_action(row, action_rows) if include_actions else None,
                    "ops_desk_top_action": _dashboard_ops_desk_action(stakeholder_scores) if include_actions else None,
                    "action_count": len(action_rows),
                    "stakeholder_count": len(stakeholder_scores),
                    "actor_count": len(row.get("actor_scores") or []),
                    "detail_url": (
                        "http://127.0.0.1:8000/analytics/capabilities/ui"
                        "?endpoint=%2Fanalytics%2Fcapabilities%2Fbookings%2Freview"
                        f"&booking_id={row.get('booking_id')}"
                        "&customer_days=30&max_llm_messages=12&max_llm_text_chars=220"
                        "&llm=true&include_prompt=true"
                    ),
                }
            )
        )

    columns = [
        {"key": "booking_id", "label": "Booking ID", "sort": True, "filter": "exact"},
        {"key": "booking.status", "label": "Booking status", "sort": True, "filter": "status"},
        {"key": "booking.current_state", "label": "Current state", "sort": True, "filter": "status"},
        {"key": "booking.property_name", "label": "Property", "sort": True, "filter": "text"},
        {"key": "booking.building_id", "label": "Building", "sort": True, "filter": "exact"},
        {"key": "stakeholders.sales_owner", "label": "Sales"},
        {"key": "stakeholders.caretaker", "label": "Caretaker"},
        {"key": "stakeholders.ops_owner", "label": "Ops"},
        {"key": "rating.overall_score", "label": "Overall score", "sort": True, "filter": "range"},
        {"key": "rating.work_priority_score", "label": "Work priority", "sort": True, "filter": "range"},
        {"key": "rating.overall_priority_score", "label": "LLM priority", "sort": True, "filter": "range"},
        {"key": "rating.customer_perspective_score", "label": "Customer score", "sort": True},
        {"key": "rating.operations_score", "label": "Ops score", "sort": True},
        {"key": "rating.support_score", "label": "Support score", "sort": True},
        {"key": "counts.open_tickets", "label": "Open tickets"},
        {"key": "top_action.action", "label": "Top action"},
        {"key": "rating.updated_at", "label": "Rated at", "sort": True},
    ]

    return compact_dict(
        {
            "view": "booking_dashboard",
            "context_version": BOOKING_REVIEW_CONTEXT_VERSION,
            "source_table": BOOKING_LLM_REVIEW_CACHE_TABLE,
            "only_rated": only_rated,
            "limit": params["limit_n"],
            "offset": params["offset_n"],
            "total": total,
            "count": len(dashboard_rows),
            "sort": {"by": sort_by, "dir": direction.lower()},
            "filters": compact_dict(
                {
                    "booking_id": booking_id,
                    "status": status,
                    "current_state": current_state,
                    "property_name": property_name,
                    "building_id": building_id,
                    "owner": owner,
                    "review_status": review_status,
                    "min_score": min_score,
                    "max_score": max_score,
                    "min_priority": min_priority,
                    "max_priority": max_priority,
                    "stay_from": stay_from,
                    "stay_to": stay_to,
                    "search": search,
                }
            ),
            "columns": columns,
            "rows": dashboard_rows,
        }
    )


def list_booking_action_queue(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    slice_type: Optional[str] = None,
    slice_key: Optional[str] = None,
    owner_team: Optional[str] = None,
    min_priority: Optional[float] = None,
    include_no_action: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    """Return action-level work queue from booking_review_action_queue_v.

    This is the same booking review intelligence, sliced by ops_manager,
    finance_manager, caretaker, building, property, owner_team, or all.
    """
    view_name = "booking_review_action_queue_v"
    if not table_exists(db, schema, view_name):
        raise ValueError(f"{view_name} view is required. Run booking_review_cache_scope_work_migration.sql first.")

    params: Dict[str, Any] = {
        "limit_n": max(1, min(int(limit or 100), 1000)),
        "offset_n": max(0, int(offset or 0)),
    }
    where_parts: List[str] = []

    normalized_slice_type = str(slice_type or "").strip().lower()
    if normalized_slice_type and slice_key not in (None, ""):
        if normalized_slice_type in {"ops_manager", "ops"}:
            where_parts.append("LOWER(COALESCE(ops_manager, ops_owner, '')) = :slice_key")
            params["slice_key"] = str(slice_key).strip().lower()
        elif normalized_slice_type in {"finance_manager", "finance"}:
            where_parts.append("LOWER(COALESCE(finance_manager, finance_owner, '')) = :slice_key")
            params["slice_key"] = str(slice_key).strip().lower()
        elif normalized_slice_type == "caretaker":
            where_parts.append("LOWER(COALESCE(caretaker, '')) = :slice_key")
            params["slice_key"] = str(slice_key).strip().lower()
        elif normalized_slice_type == "building":
            where_parts.append("building_id::text = :slice_key_text")
            params["slice_key_text"] = str(slice_key).strip()
        elif normalized_slice_type == "property":
            where_parts.append("property_id::text = :slice_key_text")
            params["slice_key_text"] = str(slice_key).strip()
        else:
            raise ValueError("slice_type must be one of ops_manager, finance_manager, caretaker, building, property, or omitted")

    if owner_team:
        where_parts.append("COALESCE(owner_team, '') ILIKE :owner_team")
        params["owner_team"] = f"%{str(owner_team).strip()}%"
    if min_priority is not None:
        where_parts.append("COALESCE(action_priority_score, work_priority_score, 0) >= :min_priority")
        params["min_priority"] = float(min_priority)
    if not include_no_action:
        where_parts.append("COALESCE(action_is_no_action, false) = false")

    where_sql = "WHERE " + " AND ".join(where_parts) if where_parts else ""
    rows = q(
        db,
        f"""
        SELECT *, COUNT(*) OVER() AS _total_count
        FROM {table_ref(schema, view_name)}
        {where_sql}
        ORDER BY
            COALESCE(action_priority_score, work_priority_score, 0) DESC NULLS LAST,
            CASE LOWER(COALESCE(overall_risk, '')) WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END DESC,
            overall_score ASC NULLS LAST,
            updated_at DESC NULLS LAST,
            booking_id DESC
        LIMIT :limit_n OFFSET :offset_n
        """,
        params,
    )
    total = int(rows[0].get("_total_count") or 0) if rows else 0
    for row in rows:
        row.pop("_total_count", None)
    return compact_dict(
        {
            "view": "booking_action_queue",
            "slice_type": slice_type,
            "slice_key": slice_key,
            "owner_team": owner_team,
            "min_priority": min_priority,
            "include_no_action": include_no_action,
            "total": total,
            "count": len(rows),
            "limit": params["limit_n"],
            "offset": params["offset_n"],
            "rows": rows,
        }
    )


def list_booking_building_summary(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    """Return building-level ranking from cached booking reviews."""
    view_name = "booking_review_building_summary_v"
    if not table_exists(db, schema, view_name):
        raise ValueError(f"{view_name} view is required. Run booking_review_cache_scope_work_migration.sql first.")
    params = {
        "limit_n": max(1, min(int(limit or 100), 1000)),
        "offset_n": max(0, int(offset or 0)),
    }
    rows = q(
        db,
        f"""
        SELECT *, COUNT(*) OVER() AS _total_count
        FROM {table_ref(schema, view_name)}
        ORDER BY urgent_count DESC, avg_work_priority DESC NULLS LAST, avg_score ASC NULLS LAST
        LIMIT :limit_n OFFSET :offset_n
        """,
        params,
    )
    total = int(rows[0].get("_total_count") or 0) if rows else 0
    for row in rows:
        row.pop("_total_count", None)
    return {
        "view": "booking_building_summary",
        "total": total,
        "count": len(rows),
        "limit": params["limit_n"],
        "offset": params["offset_n"],
        "rows": rows,
    }
