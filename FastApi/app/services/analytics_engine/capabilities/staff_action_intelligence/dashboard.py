from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from app.services.analytics_engine.capabilities.booking_management.common import DEFAULT_SCHEMA, compact_dict, coerce_float

from .cache import list_staff_action_reviews
from .rules import risk_sort_value
from app.services.analytics_engine.capabilities.staff_activity_common import now_ist_naive


SORTABLE_FIELDS = {
    "priority_score",
    "overall_score",
    "risk",
    "rated_at",
    "username",
    "team",
    "status",
}
STATUS_ORDER = {"open": 0, "assigned": 1, "resolved": 2, "verified": 3, "ignored": 4}


def normalize_dashboard_sort(sort_by: Any, sort_dir: Any) -> tuple[str, str]:
    sort_field = str(sort_by or "priority_score").strip().lower()
    if sort_field not in SORTABLE_FIELDS:
        sort_field = "priority_score"
    direction = str(sort_dir or "desc").strip().lower()
    if direction not in {"asc", "desc"}:
        direction = "desc"
    return sort_field, direction


def _review_sort_key(row: dict[str, Any], sort_by: str) -> Any:
    staff = row.get("staff") if isinstance(row.get("staff"), dict) else {}
    rating = row.get("rating") if isinstance(row.get("rating"), dict) else {}

    if sort_by == "risk":
        return (
            risk_sort_value(rating.get("risk")),
            -(coerce_float(rating.get("priority_score")) or -1.0),
            -(coerce_float(rating.get("overall_score")) or -1.0),
            str(staff.get("username") or "").lower(),
        )
    if sort_by == "priority_score":
        return (
            coerce_float(rating.get("priority_score")) if rating.get("priority_score") is not None else float("-inf"),
            risk_sort_value(rating.get("risk")),
            str(staff.get("username") or "").lower(),
        )
    if sort_by == "overall_score":
        return (
            coerce_float(rating.get("overall_score")) if rating.get("overall_score") is not None else float("-inf"),
            coerce_float(rating.get("priority_score")) if rating.get("priority_score") is not None else float("-inf"),
            str(staff.get("username") or "").lower(),
        )
    if sort_by == "rated_at":
        return row.get("rated_at") or datetime.min
    if sort_by == "username":
        return str(staff.get("username") or "").lower()
    if sort_by == "team":
        return (
            str(staff.get("team") or "").lower(),
            str(staff.get("role_scope") or "").lower(),
            str(staff.get("username") or "").lower(),
        )
    if sort_by == "status":
        return (
            STATUS_ORDER.get(str(row.get("status") or "").strip().lower(), 99),
            -(coerce_float(rating.get("priority_score")) or -1.0),
            str(staff.get("username") or "").lower(),
        )
    return (
        coerce_float(rating.get("priority_score")) if rating.get("priority_score") is not None else float("-inf"),
        risk_sort_value(rating.get("risk")),
        str(staff.get("username") or "").lower(),
    )


def sort_dashboard_rows(rows: list[dict[str, Any]], *, sort_by: str = "priority_score", sort_dir: str = "desc") -> list[dict[str, Any]]:
    normalized_sort, normalized_dir = normalize_dashboard_sort(sort_by, sort_dir)
    if normalized_sort == "risk":
        return sorted(
            list(rows or []),
            key=lambda row: _review_sort_key(row, normalized_sort),
            reverse=normalized_dir == "asc",
        )
    if normalized_sort == "status":
        return sorted(
            list(rows or []),
            key=lambda row: _review_sort_key(row, normalized_sort),
            reverse=normalized_dir == "asc",
        )
    reverse = normalized_dir == "desc"
    return sorted(list(rows or []), key=lambda row: _review_sort_key(row, normalized_sort), reverse=reverse)


def _build_detail_url(staff: dict[str, Any], days: int) -> Optional[str]:
    params: dict[str, Any] = {"role": "auto", "days": int(days or 7), "display_mode": "evidence", "llm": "true"}
    if staff.get("username"):
        params["username"] = staff.get("username")
    elif staff.get("email"):
        params["email"] = staff.get("email")
    elif staff.get("phone"):
        params["phone"] = staff.get("phone")
    else:
        return None
    return f"/analytics/capabilities/staff/activity?{urlencode(params)}"


def build_staff_action_dashboard_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    now = now_ist_naive()
    high_risk_count = 0
    medium_risk_count = 0
    low_risk_count = 0
    no_data_count = 0
    urgent_actions_count = 0
    overdue_actions_count = 0
    missed_call_risk_count = 0
    ticket_sla_breach_count = 0
    revenue_risk_count = 0
    team_breakdown: dict[str, int] = {}

    for row in rows or []:
        staff = row.get("staff") if isinstance(row.get("staff"), dict) else {}
        rating = row.get("rating") if isinstance(row.get("rating"), dict) else {}
        impact = row.get("business_impact") if isinstance(row.get("business_impact"), dict) else {}
        evidence_counts = row.get("evidence_counts") if isinstance(row.get("evidence_counts"), dict) else {}
        team_name = str(staff.get("team") or staff.get("role_scope") or "Unknown").strip() or "Unknown"
        team_breakdown[team_name] = team_breakdown.get(team_name, 0) + 1

        risk_text = str(rating.get("risk") or "").strip().lower()
        if risk_text == "high":
            high_risk_count += 1
        elif risk_text == "medium":
            medium_risk_count += 1
        elif risk_text == "low":
            low_risk_count += 1
        else:
            no_data_count += 1

        if int(evidence_counts.get("unrecovered_missed_calls_count") or 0) > 0:
            missed_call_risk_count += 1
        ticket_sla_breach_count += int(evidence_counts.get("ticket_sla_breach_count") or 0)
        if impact.get("revenue_risk") or int(evidence_counts.get("finance_pending_rows") or 0) > 0:
            revenue_risk_count += 1

        for action in row.get("recommended_actions") or []:
            if not isinstance(action, dict):
                continue
            status_text = str(action.get("status") or "open").strip().lower()
            priority_text = str(action.get("priority") or "").strip().lower()
            if priority_text == "high" and status_text not in {"resolved", "verified", "ignored"}:
                urgent_actions_count += 1
            due_by_at = action.get("due_by_at")
            if due_by_at and status_text not in {"resolved", "verified", "ignored"}:
                try:
                    if datetime.fromisoformat(str(due_by_at)) < now:
                        overdue_actions_count += 1
                except Exception:
                    pass

    return {
        "total_staff_reviewed": len(rows or []),
        "high_risk_count": high_risk_count,
        "medium_risk_count": medium_risk_count,
        "low_risk_count": low_risk_count,
        "no_data_count": no_data_count,
        "urgent_actions_count": urgent_actions_count,
        "overdue_actions_count": overdue_actions_count,
        "missed_call_risk_count": missed_call_risk_count,
        "ticket_sla_breach_count": ticket_sla_breach_count,
        "revenue_risk_count": revenue_risk_count,
        "team_breakdown": team_breakdown,
    }


def _dashboard_row(review: dict[str, Any], *, days: int) -> dict[str, Any]:
    staff = review.get("staff") if isinstance(review.get("staff"), dict) else {}
    rating = review.get("rating") if isinstance(review.get("rating"), dict) else {}
    return compact_dict(
        {
            "id": review.get("id"),
            "staff": staff,
            "rating": rating,
            "business_impact": review.get("business_impact") or {},
            "key_findings": review.get("key_findings") or [],
            "recommended_actions": review.get("recommended_actions") or [],
            "coaching_points": review.get("coaching_points") or [],
            "data_gaps": review.get("data_gaps") or [],
            "evidence_counts": review.get("evidence_counts") or {},
            "status": review.get("status") or "open",
            "rated_at": review.get("rated_at"),
            "detail_url": _build_detail_url(staff, days),
        }
    )


def list_staff_action_dashboard_rows(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    team: Optional[str] = None,
    risk: Optional[str] = None,
    status: Optional[str] = None,
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
    min_priority: Optional[float] = None,
    max_priority: Optional[float] = None,
    search: Optional[str] = None,
    days: int = 7,
    limit: int = 100,
    offset: int = 0,
    sort_by: str = "priority_score",
    sort_dir: str = "desc",
) -> dict[str, Any]:
    reviews = list_staff_action_reviews(
        db,
        schema,
        days=days,
        team=team,
        risk=risk,
        status=status,
        min_score=min_score,
        max_score=max_score,
        min_priority=min_priority,
        max_priority=max_priority,
        search=search,
    )
    summary = build_staff_action_dashboard_summary(reviews)
    sorted_rows = sort_dashboard_rows(reviews, sort_by=sort_by, sort_dir=sort_dir)
    total = len(sorted_rows)
    sliced_reviews = sorted_rows[int(offset) : int(offset) + int(limit)]

    return {
        "view": "staff_action_intelligence_dashboard",
        "days": int(days or 7),
        "total": total,
        "limit": int(limit),
        "offset": int(offset),
        "summary": summary,
        "rows": [_dashboard_row(review, days=days) for review in sliced_reviews],
    }
