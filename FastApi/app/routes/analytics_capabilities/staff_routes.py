from __future__ import annotations

import json
from typing import Any, Literal, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import HTMLResponse
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.routes.analytics_capabilities.common import (
    DEFAULT_SCHEMA,
    StaffActivityReviewRequest,
    _api_error,
    _encode,
    _with_copy_blocks,
)
from app.services.analytics_engine.capabilities.caretaker_performance.dashboard import (
    list_caretaker_performance_dashboard_rows,
)
from app.services.analytics_engine.capabilities.caretaker_performance.jobs import (
    rate_caretakers_batch,
)
from app.services.analytics_engine.capabilities.caretaker_performance.rating_runner import (
    CaretakerPerformanceReviewRunner,
)
from app.services.analytics_engine.capabilities.staff_action_intelligence import (
    ACTION_STATUS_VALUES,
    StaffActionIntelligenceService,
    list_staff_action_dashboard_rows,
    update_staff_action_status,
)
from app.services.analytics_engine.capabilities.staff_activity_review import StaffActivityReviewService


router = APIRouter()


class StaffActionReviewRequestBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(default=DEFAULT_SCHEMA, alias="schema")
    username: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    role: str = "auto"
    days: int = Field(default=7, ge=1, le=365)
    limit: int = Field(default=10000, ge=1, le=100000)
    print_limit: int = Field(default=50, ge=0, le=5000)
    max_text: int = Field(default=160, ge=40, le=2000)
    run_llm: bool = False
    force_refresh: bool = False
    model: str = "gpt-5-mini"
    timeout_seconds: int = Field(default=120, ge=10, le=600)

    @model_validator(mode="after")
    def validate_seed(self):
        active = [name for name in ("username", "email", "phone") if getattr(self, name, None) not in (None, "")]
        if len(active) != 1:
            raise ValueError(f"Provide exactly one of username, email, or phone. Got: {active or 'none'}")
        return self


class StaffActionBatchRequestBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(default=DEFAULT_SCHEMA, alias="schema")
    team: Optional[str] = None
    active: bool = True
    days: int = Field(default=7, ge=1, le=365)
    limit: int = Field(default=100, ge=1, le=1000)
    run_llm: bool = False
    force_refresh: bool = False
    fail_fast: bool = False
    model: str = "gpt-5-mini"
    timeout_seconds: int = Field(default=120, ge=10, le=600)


class StaffActionStatusPatchBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(default=DEFAULT_SCHEMA, alias="schema")
    review_id: Optional[int] = None
    staff_key: Optional[str] = None
    action_index: int = Field(ge=0)
    status: str
    window_days: int = Field(default=7, ge=1, le=365)
    role_scope: Optional[str] = None

    @model_validator(mode="after")
    def validate_target(self):
        if self.review_id in (None, "") and self.staff_key in (None, ""):
            raise ValueError("Provide review_id or staff_key.")
        if str(self.status or "").strip().lower() not in ACTION_STATUS_VALUES:
            raise ValueError(f"status must be one of {sorted(ACTION_STATUS_VALUES)}")
        return self


class StaffActionAssistantRequestBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(default=DEFAULT_SCHEMA, alias="schema")
    review_id: Optional[int] = None
    username: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    question: str
    role: str = "auto"
    days: int = Field(default=7, ge=1, le=365)
    limit: int = Field(default=10000, ge=1, le=100000)
    print_limit: int = Field(default=50, ge=0, le=5000)
    max_text: int = Field(default=160, ge=40, le=2000)
    model: str = "gpt-5-mini"
    timeout_seconds: int = Field(default=120, ge=10, le=600)

    @model_validator(mode="after")
    def validate_target(self):
        has_seed = any(getattr(self, name, None) not in (None, "") for name in ("username", "email", "phone"))
        if self.review_id in (None, "") and not has_seed:
            raise ValueError("Provide review_id or one of username/email/phone.")
        if not str(self.question or "").strip():
            raise ValueError("question is required.")
        return self


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}, ()):  # keep 0 / False
            return value
    return None


def _pick_staff_value(row: dict[str, Any], keys: list[str]) -> Any:
    sources = [
        row.get("staff") if isinstance(row.get("staff"), dict) else {},
        row.get("profile") if isinstance(row.get("profile"), dict) else {},
        row.get("user") if isinstance(row.get("user"), dict) else {},
        row,
    ]
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if value not in (None, "", [], {}, ()):  # keep 0 / False
                return value
    return None


def _dashboard_account_type_label(value: Any) -> str:
    text_value = str(value).strip().lower()
    if value is True or text_value in {"1", "true", "t", "yes", "y", "admin"}:
        return "Admin"
    return "User"


def _staff_row_key(row: dict[str, Any]) -> str:
    username = _pick_staff_value(row, ["username", "user_name", "login", "executive_id", "staff_username"])
    email = _pick_staff_value(row, ["email", "email_id", "staff_email"])
    phone = _pick_staff_value(row, ["phone", "mobile", "staff_phone", "phone_number", "admin_number", "number"])
    source_id = _pick_staff_value(row, ["staff_id", "user_id", "source_id", "id"])
    return str(_first_non_empty(username, email, phone, source_id, json.dumps(row, sort_keys=True, default=str)) or "")


def _normalize_staff_dashboard_row(row: dict[str, Any], *, days: int) -> dict[str, Any]:
    username = _pick_staff_value(row, ["username", "user_name", "login", "executive_id", "staff_username"])
    email = _pick_staff_value(row, ["email", "email_id", "staff_email"])
    phone = _pick_staff_value(row, ["phone", "mobile", "staff_phone", "phone_number", "admin_number", "number"])
    display_name = _pick_staff_value(row, ["display_name", "name", "full_name", "staff_name", "executive_name", "username"])
    team = _pick_staff_value(row, ["team_name", "team", "department", "department_name"])
    role = _first_non_empty(
        _pick_staff_value(row, ["role", "actor_role", "staff_role", "team_name", "team", "department"]),
        team,
        _dashboard_account_type_label(_pick_staff_value(row, ["is_admin", "admin", "account_type"])),
    )
    active = _pick_staff_value(row, ["active", "is_active", "enabled", "login_status", "status"])
    city = _pick_staff_value(row, ["city", "location"])

    seed_type = None
    seed_value = None
    if username not in (None, ""):
        seed_type, seed_value = "username", username
    elif email not in (None, ""):
        seed_type, seed_value = "email", email
    elif phone not in (None, ""):
        seed_type, seed_value = "phone", phone

    query_parts = {
        seed_type: seed_value,
        "role": "auto",
        "days": int(days),
        "llm": "true",
        "display_mode": "evidence",
    } if seed_type and seed_value not in (None, "") else {}
    query = urlencode(query_parts) if query_parts else ""

    return {
        "staff_key": _staff_row_key(row),
        "display_name": _first_non_empty(display_name, username, email, phone, "Unknown staff"),
        "username": username,
        "email": email,
        "phone": phone,
        "team": team,
        "role": role,
        "active": active,
        "city": city,
        "seed_type": seed_type,
        "seed_value": seed_value,
        "activity_url": f"/analytics/capabilities/staff/activity?{query}" if query else None,
        "detail_url": f"/analytics/capabilities/ui?endpoint=%2Fanalytics%2Fcapabilities%2Fstaff%2Factivity&{query}" if query else None,
        "raw": row,
    }


def _staff_search_blob(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, default=str).lower()


def _as_bool_text(value: Optional[str], *, default: str = "true") -> str:
    text_value = str(value if value is not None else default).strip().lower()
    if text_value in {"", "any", "all", "none"}:
        return "all"
    if text_value in {"1", "true", "t", "yes", "y", "active"}:
        return "true"
    if text_value in {"0", "false", "f", "no", "n", "inactive"}:
        return "false"
    return text_value


@router.get("/staff/profile")
def staff_profile_get(
    username: Optional[str] = Query(default=None),
    email: Optional[str] = Query(default=None),
    phone: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
) -> Any:
    try:
        service = StaffActivityReviewService(db=db, schema=DEFAULT_SCHEMA)
        return _encode(service.resolve_staff(username=username, email=email, phone=phone))
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/staff/list")
def staff_list_get(
    team: Optional[str] = Query(default=None, description="Example: Caretaker, Sales, Finance, Ops Team"),
    active: bool = Query(default=True),
    limit: int = Query(default=500, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> Any:
    try:
        service = StaffActivityReviewService(db=db, schema=DEFAULT_SCHEMA)
        staff_rows = service.list_staff(team=team, active=active, limit=limit)
        return _encode({"team": team, "active": active, "count": len(staff_rows), "staff": staff_rows})
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/staff/activity/dashboard")
def staff_activity_dashboard_get(
    team: Optional[str] = Query(default=None, description="Example: Caretaker, Sales, Finance, Ops Team. Empty = all teams."),
    active: Optional[str] = Query(default="true", description="true, false, or all."),
    search: Optional[str] = Query(default=None, description="Search name, username, phone, email, team, or raw staff fields."),
    days: int = Query(default=3, ge=1, le=365),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> Any:
    """Staff dashboard JSON.

    This intentionally reads the staff directory only. It does not call the LLM
    for every staff member. The dashboard page calls /staff/activity for one
    selected staff member when a detailed review/prompt is needed.
    """
    try:
        service = StaffActivityReviewService(db=db, schema=DEFAULT_SCHEMA)
        active_text = _as_bool_text(active)
        fetch_limit = min(5000, max(int(limit) + int(offset), int(limit), 100))

        raw_rows: list[dict[str, Any]] = []
        if active_text == "all":
            for active_value in (True, False):
                for row in service.list_staff(team=team, active=active_value, limit=fetch_limit):
                    if isinstance(row, dict):
                        raw_rows.append(row)
        else:
            active_bool = active_text != "false"
            raw_rows = [row for row in service.list_staff(team=team, active=active_bool, limit=fetch_limit) if isinstance(row, dict)]

        # Dedupe because active=all intentionally merges active and inactive lists.
        deduped: dict[str, dict[str, Any]] = {}
        for row in raw_rows:
            deduped.setdefault(_staff_row_key(row), row)

        normalized_rows = [_normalize_staff_dashboard_row(row, days=days) for row in deduped.values()]
        if search:
            search_text = str(search).strip().lower()
            normalized_rows = [row for row in normalized_rows if search_text in _staff_search_blob(row)]

        normalized_rows.sort(
            key=lambda row: (
                str(row.get("team") or row.get("role") or "").lower(),
                str(row.get("display_name") or "").lower(),
                str(row.get("username") or row.get("phone") or row.get("email") or "").lower(),
            )
        )
        total = len(normalized_rows)
        sliced = normalized_rows[int(offset): int(offset) + int(limit)]
        return _encode(
            {
                "view": "staff_activity_dashboard",
                "title": "Staff Activity Review Dashboard",
                "team": team,
                "active": active_text,
                "days": days,
                "total": total,
                "limit": limit,
                "offset": offset,
                "rows": sliced,
                "note": "List is fast and does not call the LLM. Select one staff member to load Staff Activity Review evidence and LLM prompt.",
            }
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/staff/activity")
def staff_activity(payload: StaffActivityReviewRequest, db: Session = Depends(get_db)) -> Any:
    try:
        service = StaffActivityReviewService(db=db, schema=payload.schema_name)
        result = service.build_staff_activity(
            username=payload.username,
            email=payload.email,
            phone=payload.phone,
            role=payload.role or "auto",
            days=payload.days,
            limit=payload.limit,
            print_limit=payload.print_limit,
            max_text=payload.max_text,
            llm=payload.llm,
            display_mode=payload.display_mode,
        )
        if result.get("llm_prompt"):
            _with_copy_blocks(result)
        return _encode(result)
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/staff/activity")
def staff_activity_get(
    username: Optional[str] = Query(default=None, description="Staff username."),
    email: Optional[str] = Query(default=None, description="Staff email."),
    phone: Optional[str] = Query(
        default=None,
        description="Staff phone. Accepts 10 or 12 digits; matching uses last 10 digits.",
    ),
    role: str = Query(default="auto", description="Use auto. Backend resolves role from staging_user_account.team."),
    days: int = Query(default=3, ge=1, le=365),
    limit: int = Query(default=10000, ge=1, le=100000),
    print_limit: int = Query(default=50, ge=0, le=5000),
    max_text: int = Query(default=160, ge=40, le=2000),
    llm: bool = Query(default=True),
    display_mode: Literal["evidence", "llm", "raw"] = Query(default="evidence"),
    db: Session = Depends(get_db),
) -> Any:
    payload = StaffActivityReviewRequest(
        username=username,
        email=email,
        phone=phone,
        role=role or "auto",
        days=days,
        limit=limit,
        print_limit=print_limit,
        max_text=max_text,
        llm=llm,
        display_mode=display_mode,
        schema=DEFAULT_SCHEMA,
    )
    return staff_activity(payload, db)


def _staff_action_review_payload_from_inputs(
    *,
    body: Optional[StaffActionReviewRequestBody],
    username: Optional[str],
    email: Optional[str],
    phone: Optional[str],
    role: str,
    days: int,
    limit: int,
    print_limit: int,
    max_text: int,
    run_llm: bool,
    force_refresh: bool,
    model: str,
    timeout_seconds: int,
) -> StaffActionReviewRequestBody:
    if body is not None:
        return body
    return StaffActionReviewRequestBody(
        username=username,
        email=email,
        phone=phone,
        role=role or "auto",
        days=days,
        limit=limit,
        print_limit=print_limit,
        max_text=max_text,
        run_llm=run_llm,
        force_refresh=force_refresh,
        model=model,
        timeout_seconds=timeout_seconds,
        schema=DEFAULT_SCHEMA,
    )


def _staff_action_batch_payload_from_inputs(
    *,
    body: Optional[StaffActionBatchRequestBody],
    team: Optional[str],
    active: bool,
    days: int,
    limit: int,
    run_llm: bool,
    force_refresh: bool,
    fail_fast: bool,
    model: str,
    timeout_seconds: int,
) -> StaffActionBatchRequestBody:
    if body is not None:
        return body
    return StaffActionBatchRequestBody(
        team=team,
        active=active,
        days=days,
        limit=limit,
        run_llm=run_llm,
        force_refresh=force_refresh,
        fail_fast=fail_fast,
        model=model,
        timeout_seconds=timeout_seconds,
        schema=DEFAULT_SCHEMA,
    )


def _staff_action_status_payload_from_inputs(
    *,
    body: Optional[StaffActionStatusPatchBody],
    review_id: Optional[int],
    staff_key: Optional[str],
    action_index: int,
    status: str,
    window_days: int,
    role_scope: Optional[str],
) -> StaffActionStatusPatchBody:
    if body is not None:
        return body
    return StaffActionStatusPatchBody(
        review_id=review_id,
        staff_key=staff_key,
        action_index=action_index,
        status=status,
        window_days=window_days,
        role_scope=role_scope,
        schema=DEFAULT_SCHEMA,
    )


def _staff_action_assistant_payload_from_inputs(
    *,
    body: Optional[StaffActionAssistantRequestBody],
    review_id: Optional[int],
    username: Optional[str],
    email: Optional[str],
    phone: Optional[str],
    question: str,
    role: str,
    days: int,
    limit: int,
    print_limit: int,
    max_text: int,
    model: str,
    timeout_seconds: int,
) -> StaffActionAssistantRequestBody:
    if body is not None:
        return body
    return StaffActionAssistantRequestBody(
        review_id=review_id,
        username=username,
        email=email,
        phone=phone,
        question=question,
        role=role or "auto",
        days=days,
        limit=limit,
        print_limit=print_limit,
        max_text=max_text,
        model=model,
        timeout_seconds=timeout_seconds,
        schema=DEFAULT_SCHEMA,
    )


@router.get("/staff/action-dashboard")
def staff_action_dashboard_get(
    team: Optional[str] = Query(default=None),
    risk: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    min_score: Optional[float] = Query(default=None),
    max_score: Optional[float] = Query(default=None),
    min_priority: Optional[float] = Query(default=None),
    max_priority: Optional[float] = Query(default=None),
    search: Optional[str] = Query(default=None),
    days: int = Query(default=7, ge=1, le=365),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="priority_score"),
    sort_dir: str = Query(default="desc"),
    db: Session = Depends(get_db),
) -> Any:
    try:
        return _encode(
            list_staff_action_dashboard_rows(
                db,
                DEFAULT_SCHEMA,
                team=team,
                risk=risk,
                status=status,
                min_score=min_score,
                max_score=max_score,
                min_priority=min_priority,
                max_priority=max_priority,
                search=search,
                days=days,
                limit=limit,
                offset=offset,
                sort_by=sort_by,
                sort_dir=sort_dir,
            )
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/staff/action-dashboard/summary")
def staff_action_dashboard_summary_get(
    team: Optional[str] = Query(default=None),
    risk: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    min_score: Optional[float] = Query(default=None),
    max_score: Optional[float] = Query(default=None),
    min_priority: Optional[float] = Query(default=None),
    max_priority: Optional[float] = Query(default=None),
    search: Optional[str] = Query(default=None),
    days: int = Query(default=7, ge=1, le=365),
    db: Session = Depends(get_db),
) -> Any:
    try:
        dashboard = list_staff_action_dashboard_rows(
            db,
            DEFAULT_SCHEMA,
            team=team,
            risk=risk,
            status=status,
            min_score=min_score,
            max_score=max_score,
            min_priority=min_priority,
            max_priority=max_priority,
            search=search,
            days=days,
            limit=100000,
            offset=0,
        )
        return _encode(
            {
                "view": "staff_action_intelligence_dashboard_summary",
                "days": days,
                "summary": dashboard.get("summary") or {},
            }
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/staff/action-review")
def staff_action_review_post(
    body: Optional[StaffActionReviewRequestBody] = Body(default=None),
    username: Optional[str] = Query(default=None),
    email: Optional[str] = Query(default=None),
    phone: Optional[str] = Query(default=None),
    role: str = Query(default="auto"),
    days: int = Query(default=7, ge=1, le=365),
    limit: int = Query(default=10000, ge=1, le=100000),
    print_limit: int = Query(default=50, ge=0, le=5000),
    max_text: int = Query(default=160, ge=40, le=2000),
    run_llm: bool = Query(default=False),
    force_refresh: bool = Query(default=False),
    model: str = Query(default="gpt-5-mini"),
    timeout_seconds: int = Query(default=120, ge=10, le=600),
    db: Session = Depends(get_db),
) -> Any:
    try:
        payload = _staff_action_review_payload_from_inputs(
            body=body,
            username=username,
            email=email,
            phone=phone,
            role=role,
            days=days,
            limit=limit,
            print_limit=print_limit,
            max_text=max_text,
            run_llm=run_llm,
            force_refresh=force_refresh,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        service = StaffActionIntelligenceService(db=db, schema=payload.schema_name)
        result = service.build_review(
            username=payload.username,
            email=payload.email,
            phone=payload.phone,
            role=payload.role,
            days=payload.days,
            limit=payload.limit,
            print_limit=payload.print_limit,
            max_text=payload.max_text,
            run_llm=payload.run_llm,
            force_refresh=payload.force_refresh,
            model=payload.model,
            timeout_seconds=payload.timeout_seconds,
        )
        return _encode(result)
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/staff/action-review/assistant")
def staff_action_review_assistant_post(
    body: Optional[StaffActionAssistantRequestBody] = Body(default=None),
    review_id: Optional[int] = Query(default=None),
    username: Optional[str] = Query(default=None),
    email: Optional[str] = Query(default=None),
    phone: Optional[str] = Query(default=None),
    question: str = Query(default=""),
    role: str = Query(default="auto"),
    days: int = Query(default=7, ge=1, le=365),
    limit: int = Query(default=10000, ge=1, le=100000),
    print_limit: int = Query(default=50, ge=0, le=5000),
    max_text: int = Query(default=160, ge=40, le=2000),
    model: str = Query(default="gpt-5-mini"),
    timeout_seconds: int = Query(default=120, ge=10, le=600),
    db: Session = Depends(get_db),
) -> Any:
    try:
        payload = _staff_action_assistant_payload_from_inputs(
            body=body,
            review_id=review_id,
            username=username,
            email=email,
            phone=phone,
            question=question,
            role=role,
            days=days,
            limit=limit,
            print_limit=print_limit,
            max_text=max_text,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        service = StaffActionIntelligenceService(db=db, schema=payload.schema_name)
        result = service.answer_question(
            review_id=payload.review_id,
            username=payload.username,
            email=payload.email,
            phone=payload.phone,
            question=payload.question,
            role=payload.role,
            days=payload.days,
            limit=payload.limit,
            print_limit=payload.print_limit,
            max_text=payload.max_text,
            model=payload.model,
            timeout_seconds=payload.timeout_seconds,
        )
        return _encode(result)
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/staff/action-dashboard/batch-rate")
def staff_action_dashboard_batch_rate_post(
    body: Optional[StaffActionBatchRequestBody] = Body(default=None),
    team: Optional[str] = Query(default=None),
    active: bool = Query(default=True),
    days: int = Query(default=7, ge=1, le=365),
    limit: int = Query(default=100, ge=1, le=1000),
    run_llm: bool = Query(default=False),
    force_refresh: bool = Query(default=False),
    fail_fast: bool = Query(default=False),
    model: str = Query(default="gpt-5-mini"),
    timeout_seconds: int = Query(default=120, ge=10, le=600),
    db: Session = Depends(get_db),
) -> Any:
    try:
        payload = _staff_action_batch_payload_from_inputs(
            body=body,
            team=team,
            active=active,
            days=days,
            limit=limit,
            run_llm=run_llm,
            force_refresh=force_refresh,
            fail_fast=fail_fast,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        service = StaffActionIntelligenceService(db=db, schema=payload.schema_name)
        return _encode(
            service.batch_rate(
                team=payload.team,
                active=payload.active,
                days=payload.days,
                limit=payload.limit,
                run_llm=payload.run_llm,
                force_refresh=payload.force_refresh,
                fail_fast=payload.fail_fast,
                model=payload.model,
                timeout_seconds=payload.timeout_seconds,
            )
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.patch("/staff/action-dashboard/action-status")
def staff_action_dashboard_action_status_patch(
    body: Optional[StaffActionStatusPatchBody] = Body(default=None),
    review_id: Optional[int] = Query(default=None),
    staff_key: Optional[str] = Query(default=None),
    action_index: int = Query(default=0, ge=0),
    status: str = Query(default="open"),
    window_days: int = Query(default=7, ge=1, le=365),
    role_scope: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
) -> Any:
    try:
        payload = _staff_action_status_payload_from_inputs(
            body=body,
            review_id=review_id,
            staff_key=staff_key,
            action_index=action_index,
            status=status,
            window_days=window_days,
            role_scope=role_scope,
        )
        updated = update_staff_action_status(
            db,
            payload.schema_name,
            review_id=payload.review_id,
            staff_key=payload.staff_key,
            action_index=payload.action_index,
            status=payload.status,
            window_days=payload.window_days,
            role_scope=payload.role_scope,
        )
        return _encode(updated)
    except Exception as exc:
        raise _api_error(exc) from exc


STAFF_ACTIVITY_DASHBOARD_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Staff Activity Review Dashboard</title>
  <style>
    :root { color-scheme: light; --bg:#f7f8fa; --card:#fff; --border:#dfe3ea; --text:#172033; --muted:#667085; --bad:#b42318; --mid:#b54708; --ok:#067647; --blue:#175cd3; }
    body { margin:0; font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:var(--bg); color:var(--text); }
    header { padding:22px 28px 12px; }
    h1 { margin:0 0 6px; font-size:24px; }
    h2 { margin:0 0 8px; font-size:18px; }
    .sub { color:var(--muted); font-size:13px; }
    .panel { margin:12px 24px; background:var(--card); border:1px solid var(--border); border-radius:14px; box-shadow:0 1px 2px rgba(16,24,40,.05); }
    .filters { display:grid; grid-template-columns: repeat(7, minmax(120px, 1fr)); gap:10px; padding:14px; align-items:end; }
    label { display:block; font-size:12px; color:var(--muted); margin-bottom:4px; }
    input, select, button, textarea { width:100%; box-sizing:border-box; border:1px solid var(--border); border-radius:9px; padding:8px 10px; background:#fff; font-size:13px; }
    textarea { min-height:180px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    button { cursor:pointer; background:#172033; color:#fff; border-color:#172033; font-weight:600; }
    button.secondary { background:#fff; color:#172033; border-color:var(--border); }
    button.linkbtn { background:transparent; color:var(--blue); border:0; padding:0; width:auto; font-weight:700; }
    table { width:100%; border-collapse:collapse; font-size:13px; }
    th, td { padding:10px 12px; border-top:1px solid var(--border); text-align:left; vertical-align:top; }
    th { color:#475467; font-weight:600; background:#fbfcfd; position:sticky; top:0; z-index:1; }
    tr:hover td { background:#fcfcfd; }
    .num { text-align:right; white-space:nowrap; }
    .pill { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; border:1px solid var(--border); background:#fff; }
    .risk-high { color:var(--bad); border-color:#fda29b; background:#fff3f2; }
    .risk-medium { color:var(--mid); border-color:#fedf89; background:#fffaeb; }
    .risk-low { color:var(--ok); border-color:#abefc6; background:#ecfdf3; }
    .muted { color:var(--muted); }
    .small { font-size:12px; }
    .nowrap { white-space:nowrap; }
    .staff { min-width:220px; }
    .contact { max-width:240px; word-break:break-word; }
    .actions { display:flex; gap:8px; flex-wrap:wrap; }
    .statusbar { padding:10px 14px; display:flex; justify-content:space-between; color:var(--muted); font-size:13px; border-top:1px solid var(--border); }
    .error { color:var(--bad); }
    .split { display:grid; grid-template-columns: minmax(0, 1.2fr) minmax(420px, .8fr); gap:12px; margin:12px 24px; }
    .split .panel { margin:0; }
    .review { padding:14px; }
    .metric { display:inline-block; border:1px solid var(--border); border-radius:10px; padding:6px 8px; margin:3px 5px 3px 0; background:#fff; font-size:12px; }
    pre { white-space:pre-wrap; word-break:break-word; background:#0b1020; color:#e5e7eb; padding:12px; border-radius:10px; max-height:320px; overflow:auto; }
    @media (max-width: 1200px) { .filters { grid-template-columns: repeat(3, minmax(120px, 1fr)); } .split { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <header>
    <h1>Staff Activity Review Dashboard</h1>
    <div class="sub">Lists staff quickly from <b>/staff/list</b>. Select one staff member to load <b>/staff/activity</b> evidence + LLM prompt for rating review.</div>
  </header>

  <section class="panel">
    <form id="filters" class="filters">
      <div><label>Team</label><select name="team"><option value="">All teams</option><option>Caretaker</option><option>Sales</option><option>Finance</option><option>Ops Team</option><option>Onboarding</option><option>Technical</option><option>Marketing</option></select></div>
      <div><label>Active</label><select name="active"><option value="true">Active</option><option value="false">Inactive</option><option value="all">All</option></select></div>
      <div><label>Search</label><input name="search" placeholder="name / phone / email" /></div>
      <div><label>Review days</label><input name="days" type="number" min="1" max="365" value="3" /></div>
      <div><label>Limit</label><input name="limit" type="number" min="1" max="1000" value="100" /></div>
      <div><button type="submit">Apply filters</button></div>
      <div><button type="button" class="secondary" onclick="loadRows()">Refresh</button></div>
    </form>
  </section>

  <div class="split">
    <section class="panel">
      <div style="overflow:auto; max-height:72vh;">
        <table>
          <thead>
            <tr>
              <th class="staff">Staff</th><th>Role/team</th><th class="contact">Contact</th><th>Status</th>
              <th class="num">Score</th><th class="num">Priority</th><th>Risk</th><th>Loaded counts</th><th></th>
            </tr>
          </thead>
          <tbody id="rows"><tr><td colspan="9" class="muted">Loading…</td></tr></tbody>
        </table>
      </div>
      <div class="statusbar"><span id="status">-</span><span class="small">This table does not call LLM per row.</span></div>
    </section>

    <section class="panel review">
      <h2 id="reviewTitle">Staff review</h2>
      <div id="reviewSummary" class="muted small">Select a staff member and click Load review.</div>
      <div class="actions" style="margin:12px 0;">
        <button type="button" class="secondary" onclick="copyPrompt()">Copy LLM prompt</button>
        <button type="button" class="secondary" onclick="copyResponse()">Copy response JSON</button>
        <button type="button" class="secondary" onclick="openGenericUi()">Open in generic UI</button>
      </div>
      <label>LLM prompt / rating prompt</label>
      <textarea id="promptBox" readonly placeholder="Load a review to see the prompt."></textarea>
      <label style="margin-top:10px">Raw response</label>
      <pre id="responseBox">No staff loaded yet.</pre>
    </section>
  </div>

<script>
let dashboardRows = [];
let loadedByKey = {};
let selectedRow = null;
let selectedResponse = null;
function esc(value) { return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch])); }
function riskPill(value) { const risk = String(value || '').toLowerCase(); if (!risk) return '<span class="muted">-</span>'; return `<span class="pill risk-${esc(risk)}">${esc(value)}</span>`; }
function firstValue(objects, keys) { for (const obj of objects) { if (!obj || typeof obj !== 'object') continue; for (const key of keys) { const value = obj[key]; if (value !== undefined && value !== null && value !== '' && !(Array.isArray(value) && !value.length)) return value; } } return ''; }
function paramsFromForm() { const data = new FormData(document.getElementById('filters')); const params = new URLSearchParams(); for (const [key, value] of data.entries()) { if (String(value).trim() !== '') params.set(key, String(value).trim()); } return params; }
function activityParams(row, mode='evidence') { const form = paramsFromForm(); const params = new URLSearchParams(); if (row.seed_type && row.seed_value) params.set(row.seed_type, row.seed_value); if (!params.size) { if (row.username) params.set('username', row.username); else if (row.email) params.set('email', row.email); else if (row.phone) params.set('phone', row.phone); } params.set('role', 'auto'); params.set('days', form.get('days') || '3'); params.set('limit', '10000'); params.set('print_limit', '50'); params.set('max_text', '160'); params.set('llm', 'true'); params.set('display_mode', mode); return params; }
function genericUiUrl(row) { const params = activityParams(row, 'evidence'); return '/analytics/capabilities/ui?endpoint=%2Fanalytics%2Fcapabilities%2Fstaff%2Factivity&' + params.toString(); }
function ratingFrom(row, response) { const objects = [response || {}, (response && response.rating) || {}, (response && response.summary) || {}, row || {}, row.raw || {}]; return { score:firstValue(objects, ['overall_score','score','rating','activity_score','staff_score']), priority:firstValue(objects, ['overall_priority_score','priority_score','priority']), risk:firstValue(objects, ['overall_risk','risk','activity_risk']), reason:firstValue(objects, ['main_reason','reason','verdict']) }; }
function countsFrom(response) { if (!response || typeof response !== 'object') return {}; if (response.counts && typeof response.counts === 'object') return response.counts; if (response.summary && response.summary.counts && typeof response.summary.counts === 'object') return response.summary.counts; if (response.llm_context && response.llm_context.counts && typeof response.llm_context.counts === 'object') return response.llm_context.counts; return {}; }
function countSummary(response) { const c = countsFrom(response); const items = [['Events', c.events], ['Calls', c.calls], ['WhatsApp', c.whatsapp], ['Tickets', c.tickets ?? c.tickets_total], ['Site visits', c.site_visits], ['Assigned buildings', c.assigned_buildings], ['Vacant properties', c.vacant_properties]]; const parts = items.filter(([, value]) => value !== undefined && value !== null && value !== '').map(([label, value]) => `${label}: ${value}`); return parts.length ? parts.join('<br>') : '<span class="muted">-</span>'; }
function findPrompt(obj) { if (!obj || typeof obj !== 'object') return ''; if (obj.llm_prompt) return obj.llm_prompt; if (obj.copy_blocks && obj.copy_blocks.llm_prompt) return obj.copy_blocks.llm_prompt; if (obj.prompt) return obj.prompt; return ''; }
function renderRows() { const tbody = document.getElementById('rows'); if (!dashboardRows.length) { tbody.innerHTML = '<tr><td colspan="9" class="muted">No matching staff.</td></tr>'; return; } tbody.innerHTML = dashboardRows.map((row, idx) => { const loaded = loadedByKey[row.staff_key]; const rating = ratingFrom(row, loaded); const contact = [row.phone, row.email].filter(Boolean).map(esc).join('<br>') || '<span class="muted">-</span>'; return `<tr>
  <td><b>${esc(row.display_name)}</b><div class="small muted">${esc(row.username || row.staff_key || '')}</div></td>
  <td>${esc(row.role || row.team || '-')}<div class="small muted">${esc(row.team && row.role && row.team !== row.role ? row.team : '')}</div></td>
  <td class="contact small">${contact}</td>
  <td>${esc(row.active ?? '-')}</td>
  <td class="num"><b>${esc(rating.score || '-')}</b></td>
  <td class="num">${esc(rating.priority || '-')}</td>
  <td>${riskPill(rating.risk)}</td>
  <td class="small">${loaded ? countSummary(loaded) : '<span class="muted">not loaded</span>'}</td>
  <td><div class="actions"><button type="button" onclick="loadReview(${idx})">Load review</button><a href="${esc(genericUiUrl(row))}" target="_blank">Open</a></div></td>
</tr>`; }).join(''); }
async function loadRows() { const tbody = document.getElementById('rows'); const status = document.getElementById('status'); tbody.innerHTML = '<tr><td colspan="9" class="muted">Loading…</td></tr>'; try { const res = await fetch('dashboard?' + paramsFromForm().toString()); const data = await res.json(); if (!res.ok) throw new Error(JSON.stringify(data)); dashboardRows = data.rows || []; status.textContent = `${dashboardRows.length} shown of ${data.total || 0} staff`; renderRows(); } catch (err) { status.innerHTML = '<span class="error">Failed to load staff dashboard</span>'; tbody.innerHTML = `<tr><td colspan="9" class="error">${esc(err.message || err)}</td></tr>`; } }
async function loadReview(idx) { const row = dashboardRows[idx]; selectedRow = row; selectedResponse = null; document.getElementById('reviewTitle').textContent = `Staff review - ${row.display_name || row.username || row.phone || ''}`; document.getElementById('reviewSummary').innerHTML = '<span class="muted">Loading staff activity…</span>'; document.getElementById('promptBox').value = ''; document.getElementById('responseBox').textContent = 'Loading…'; try { const res = await fetch('/analytics/capabilities/staff/activity?' + activityParams(row, 'evidence').toString()); const data = await res.json(); if (!res.ok) throw new Error(JSON.stringify(data)); selectedResponse = data; loadedByKey[row.staff_key] = data; const rating = ratingFrom(row, data); const prompt = findPrompt(data); const counts = countsFrom(data); const reason = rating.reason ? `<div class="metric"><b>Reason</b>: ${esc(rating.reason)}</div>` : ''; document.getElementById('reviewSummary').innerHTML = `<div class="metric"><b>Score</b>: ${esc(rating.score || 'not generated')}</div><div class="metric"><b>Priority</b>: ${esc(rating.priority || 'not generated')}</div><div class="metric"><b>Risk</b>: ${esc(rating.risk || 'not generated')}</div><div class="metric"><b>Events</b>: ${esc(counts.events ?? '-')}</div><div class="metric"><b>Calls</b>: ${esc(counts.calls ?? '-')}</div><div class="metric"><b>WhatsApp</b>: ${esc(counts.whatsapp ?? '-')}</div><div class="metric"><b>Tickets</b>: ${esc(counts.tickets ?? counts.tickets_total ?? '-')}</div>${reason}<div class="small muted" style="margin-top:8px">If score/risk is blank, this endpoint returned evidence + prompt but no cached LLM rating. Use Copy LLM prompt or Open in generic UI.</div>`; document.getElementById('promptBox').value = prompt || ''; document.getElementById('responseBox').textContent = JSON.stringify(data, null, 2); renderRows(); } catch (err) { document.getElementById('reviewSummary').innerHTML = '<span class="error">Failed to load review</span>'; document.getElementById('responseBox').textContent = err.message || String(err); } }
async function copyText(text) { if (!text) return; await navigator.clipboard.writeText(text); }
function copyPrompt() { copyText(document.getElementById('promptBox').value || ''); }
function copyResponse() { copyText(selectedResponse ? JSON.stringify(selectedResponse, null, 2) : ''); }
function openGenericUi() { if (selectedRow) window.open(genericUiUrl(selectedRow), '_blank'); }
document.getElementById('filters').addEventListener('submit', event => { event.preventDefault(); loadRows(); });
loadRows();
</script>
</body>
</html>
"""


@router.get("/staff/activity/dashboard-page", response_class=HTMLResponse)
def staff_activity_dashboard_page() -> HTMLResponse:
    return HTMLResponse(STAFF_ACTIVITY_DASHBOARD_HTML)


@router.get("/staff/review/dashboard", include_in_schema=False)
def staff_review_dashboard_get(
    team: Optional[str] = Query(default=None),
    active: Optional[str] = Query(default="true"),
    search: Optional[str] = Query(default=None),
    days: int = Query(default=3, ge=1, le=365),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> Any:
    return staff_activity_dashboard_get(team=team, active=active, search=search, days=days, limit=limit, offset=offset, db=db)


@router.get("/staff/review/dashboard-page", response_class=HTMLResponse, include_in_schema=False)
def staff_review_dashboard_page() -> HTMLResponse:
    return staff_activity_dashboard_page()


@router.post("/staff/caretaker-activity")
def staff_caretaker_activity(payload: StaffActivityReviewRequest, db: Session = Depends(get_db)) -> Any:
    # Backward-compatible alias only.
    # Do NOT force Caretaker. Resolve staff first and use staging_user_account.team.
    payload.role = "auto"
    return staff_activity(payload, db)


@router.get("/staff/caretaker-activity")
def staff_caretaker_activity_get(
    phone: str = Query(..., description="Staff phone. Accepts 10 or 12 digits; matching uses last 10 digits."),
    days: int = Query(default=3, ge=1, le=365),
    limit: int = Query(default=10000, ge=1, le=100000),
    print_limit: int = Query(default=50, ge=0, le=5000),
    max_text: int = Query(default=160, ge=40, le=2000),
    llm: bool = Query(default=True),
    display_mode: Literal["evidence", "llm", "raw"] = Query(default="evidence"),
    db: Session = Depends(get_db),
) -> Any:
    payload = StaffActivityReviewRequest(
        phone=phone,
        role="auto",
        days=days,
        limit=limit,
        print_limit=print_limit,
        max_text=max_text,
        llm=llm,
        display_mode=display_mode,
        schema=DEFAULT_SCHEMA,
    )
    return staff_activity(payload, db)
@router.get("/staff/caretaker-performance/llm-rating")
def caretaker_performance_llm_rating_get(
    username: Optional[str] = Query(default=None),
    email: Optional[str] = Query(default=None),
    phone: Optional[str] = Query(default=None),
    days: int = Query(default=30, ge=1, le=365),
    run_llm: bool = Query(default=False, description="Cache-only by default. Set true to call LLM."),
    use_cache: bool = Query(default=True),
    force_refresh: bool = Query(default=False),
    model: str = Query(default="gpt-5-mini"),
    timeout_seconds: int = Query(default=120, ge=30, le=600),
    limit: int = Query(default=10000, ge=1, le=100000),
    print_limit: int = Query(default=80, ge=0, le=5000),
    max_text: int = Query(default=180, ge=40, le=2000),
    include_activity: bool = Query(default=False),
    include_prompt: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> Any:
    try:
        active = [v for v in (username, email, phone) if v not in (None, "")]
        if len(active) != 1:
            raise ValueError("Provide exactly one of username, email, or phone.")

        runner = CaretakerPerformanceReviewRunner(db=db, schema=DEFAULT_SCHEMA)
        return _encode(
            runner.review_one(
                username=username,
                email=email,
                phone=phone,
                days=days,
                model=model,
                timeout_seconds=timeout_seconds,
                limit=limit,
                print_limit=print_limit,
                max_text=max_text,
                run_llm=run_llm,
                use_cache=use_cache,
                force_refresh=force_refresh,
                include_activity=include_activity,
                include_prompt=include_prompt,
            )
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/staff/caretaker-performance/dashboard")
def caretaker_performance_dashboard_get(
    username: Optional[str] = Query(default=None),
    phone: Optional[str] = Query(default=None),
    risk: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    min_score: Optional[float] = Query(default=None, ge=0, le=10),
    max_score: Optional[float] = Query(default=None, ge=0, le=10),
    min_priority: Optional[float] = Query(default=None, ge=0, le=10),
    max_priority: Optional[float] = Query(default=None, ge=0, le=10),
    search: Optional[str] = Query(default=None),
    sort_by: Literal[
    "activity",
    "score",
    "overall_score",
    "priority",
    "priority_score",
    "risk",
    "updated_at",
    "rated_at",
    "tickets",
    "tickets_open",
    "tickets_closed",
    "calls",
    "site_visits",
    "actual_visits",
    "missed_calls",
    "connect_rate",
    "actual_visit_rate",
    "risky_site_visits",
    "booking_full",
    "window",
    "days",
] = Query(default="activity"),
    sort_dir: Literal["asc", "desc"] = Query(default="desc"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> Any:
    """
    Caretaker performance dashboard API.

    Business rule:
    - Dashboard is fixed to 30-day cached caretaker ratings.
    - Dashboard does not expose custom duration/window filters.
    - Dashboard reads cache only and does not call LLM.
    - Active and inactive caretakers are shown if they have valid cached
      30-day ratings.
    """
    try:
        return _encode(
            list_caretaker_performance_dashboard_rows(
                db=db,
                schema=DEFAULT_SCHEMA,
                username=username,
                phone=phone,
                risk=risk,
                status=status,
                min_score=min_score,
                max_score=max_score,
                min_priority=min_priority,
                max_priority=max_priority,
                search=search,
                sort_by=sort_by,
                sort_dir=sort_dir,
                limit=limit,
                offset=offset,
            )
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/staff/caretaker-performance/batch-rate")
def caretaker_performance_batch_rate_post(
    days: int = Query(default=30, ge=1, le=365),
    active: bool = Query(default=True),
    limit: int = Query(default=500, ge=1, le=5000),
    model: str = Query(default="gpt-5-mini"),
    timeout_seconds: int = Query(default=120, ge=30, le=600),
    force_refresh: bool = Query(default=False),
    run_llm: bool = Query(default=False, description="Set true only when you want LLM rating. False refreshes metrics/cache faster."),
    sleep_seconds: float = Query(default=0.0, ge=0.0, le=10.0),
    fail_fast: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> Any:
    try:
        return _encode(
            rate_caretakers_batch(
                db=db,
                schema=DEFAULT_SCHEMA,
                days=days,
                active=active,
                limit=limit,
                model=model,
                timeout_seconds=timeout_seconds,
                force_refresh=force_refresh,
                run_llm=run_llm,
                sleep_seconds=sleep_seconds,
                fail_fast=fail_fast,
            )
        )
    except Exception as exc:
        raise _api_error(exc) from exc


CARETAKER_DASHBOARD_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Caretaker Performance Dashboard</title>

  <style>
    :root {
      --bg: #f5f7fb;
      --card: #ffffff;
      --card-soft: #fbfcff;
      --border: #dde5f0;
      --border-strong: #c8d3e3;
      --text: #111827;
      --muted: #667085;
      --muted-2: #98a2b3;
      --heading: #0f172a;

      --blue: #175cd3;
      --blue-soft: #eff6ff;
      --blue-border: #bfdbfe;

      --green: #067647;
      --green-soft: #ecfdf3;
      --green-border: #abefc6;

      --orange: #b54708;
      --orange-soft: #fffaeb;
      --orange-border: #fedf89;

      --red: #b42318;
      --red-soft: #fff3f2;
      --red-border: #fda29b;

      --purple: #6941c6;
      --purple-soft: #f4f3ff;
      --purple-border: #d9d6fe;

      --gray-soft: #f8fafc;
      --shadow: 0 14px 36px rgba(15, 23, 42, 0.08);
      --shadow-soft: 0 6px 18px rgba(15, 23, 42, 0.06);

      --radius-xl: 24px;
      --radius-lg: 18px;
      --radius-md: 12px;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(23, 92, 211, 0.08), transparent 34rem),
        radial-gradient(circle at top right, rgba(6, 118, 71, 0.08), transparent 30rem),
        var(--bg);
      color: var(--text);
    }

    a {
      color: var(--blue);
      text-decoration: none;
      font-weight: 800;
    }

    a:hover {
      text-decoration: underline;
    }

    .page {
      width: min(100% - 42px, 1780px);
      margin: 0 auto;
      padding: 26px 0 34px;
    }

    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 18px;
      margin-bottom: 18px;
    }

    .eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 7px 11px;
      border: 1px solid var(--border);
      border-radius: 999px;
      color: var(--muted);
      background: rgba(255, 255, 255, 0.82);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.02em;
      margin-bottom: 10px;
      box-shadow: var(--shadow-soft);
    }

    .dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--green);
      box-shadow: 0 0 0 4px var(--green-soft);
    }

    h1 {
      margin: 0;
      color: var(--heading);
      font-size: clamp(28px, 3vw, 40px);
      line-height: 1.04;
      letter-spacing: -0.04em;
    }

    .sub {
      margin-top: 10px;
      color: var(--muted);
      font-size: 14px;
      max-width: 940px;
      line-height: 1.58;
    }

    .top-actions {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    button,
    input,
    select {
      font-family: inherit;
    }

    .refresh-btn,
    .apply-btn,
    .secondary-btn {
      border: 1px solid var(--border);
      background: var(--card);
      color: var(--text);
      border-radius: 999px;
      padding: 10px 15px;
      cursor: pointer;
      font-size: 13px;
      font-weight: 900;
      box-shadow: var(--shadow-soft);
      white-space: nowrap;
    }

    .apply-btn {
      width: 100%;
      background: #111827;
      color: #ffffff;
      border-color: #111827;
      border-radius: 12px;
      padding: 11px 14px;
    }

    .refresh-btn:hover,
    .apply-btn:hover,
    .secondary-btn:hover {
      transform: translateY(-1px);
      border-color: var(--border-strong);
    }

    .summary-grid {
      display: grid;
      grid-template-columns: repeat(5, minmax(150px, 1fr));
      gap: 12px;
      margin: 18px 0 14px;
    }

    .summary-card {
      background: rgba(255, 255, 255, 0.92);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 15px;
      box-shadow: var(--shadow-soft);
      min-height: 96px;
    }

    .summary-card.danger {
      background: var(--red-soft);
      border-color: var(--red-border);
    }

    .summary-card.warning {
      background: var(--orange-soft);
      border-color: var(--orange-border);
    }

    .summary-card.good {
      background: var(--green-soft);
      border-color: var(--green-border);
    }

    .summary-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }

    .summary-value {
      margin-top: 8px;
      font-size: 30px;
      line-height: 1;
      font-weight: 950;
      color: var(--heading);
      letter-spacing: -0.04em;
      font-variant-numeric: tabular-nums;
    }

    .summary-note {
      margin-top: 7px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }

    .panel {
      background: rgba(255, 255, 255, 0.94);
      border: 1px solid var(--border);
      border-radius: var(--radius-xl);
      box-shadow: var(--shadow);
      overflow: hidden;
      backdrop-filter: blur(10px);
    }

    .filters {
      display: grid;
      grid-template-columns: 1fr 1fr 0.85fr 0.85fr 1fr 0.8fr auto;
      gap: 12px;
      padding: 16px;
      align-items: end;
    }

    label {
      display: block;
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 6px;
      font-weight: 900;
    }

    input,
    select {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px 12px;
      background: #ffffff;
      color: var(--text);
      font-size: 13px;
      outline: none;
    }

    input:focus,
    select:focus {
      border-color: var(--blue);
      box-shadow: 0 0 0 4px var(--blue-soft);
    }

    input::placeholder {
      color: var(--muted-2);
    }

    .content-panel {
      margin-top: 14px;
    }

    .toolbar {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      padding: 15px 17px;
      border-bottom: 1px solid var(--border);
      background: linear-gradient(180deg, #ffffff, #fbfcff);
    }

    .toolbar-title {
      font-size: 16px;
      font-weight: 950;
      color: var(--heading);
    }

    .toolbar-subtitle {
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
    }

    .status-chip {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 11px;
      border-radius: 999px;
      background: var(--blue-soft);
      border: 1px solid var(--blue-border);
      color: var(--blue);
      font-size: 12px;
      font-weight: 900;
      white-space: nowrap;
    }

    .rows-wrap {
      display: grid;
      gap: 14px;
      padding: 16px;
    }

    .caretaker-card {
      border: 1px solid var(--border);
      border-radius: 22px;
      background: var(--card);
      box-shadow: var(--shadow-soft);
      overflow: hidden;
    }

    .card-head {
      display: grid;
      grid-template-columns: minmax(220px, 1.2fr) minmax(280px, 1.4fr) minmax(420px, 2fr) minmax(220px, 1fr);
      gap: 14px;
      padding: 16px;
      border-bottom: 1px solid var(--border);
      background: linear-gradient(180deg, #ffffff, #fbfcff);
    }

    .caretaker-name {
      font-size: 18px;
      font-weight: 950;
      color: var(--heading);
      letter-spacing: -0.02em;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .caretaker-meta {
      margin-top: 5px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
      word-break: break-word;
    }

    .score-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      margin-top: 12px;
    }

    .score {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 40px;
      height: 30px;
      padding: 0 9px;
      border-radius: 10px;
      background: #111827;
      color: #ffffff;
      font-weight: 950;
      font-variant-numeric: tabular-nums;
    }

    .score.light {
      color: var(--heading);
      background: var(--gray-soft);
      border: 1px solid var(--border);
    }

    .pill {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 5px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 900;
      border: 1px solid var(--border);
      white-space: nowrap;
      line-height: 1.15;
    }

    .risk-high,
    .tone-danger {
      color: var(--red);
      border-color: var(--red-border);
      background: var(--red-soft);
    }

    .risk-medium,
    .tone-warning {
      color: var(--orange);
      border-color: var(--orange-border);
      background: var(--orange-soft);
    }

    .risk-low,
    .tone-good {
      color: var(--green);
      border-color: var(--green-border);
      background: var(--green-soft);
    }

    .risk-no-data,
    .risk-nodata,
    .tone-neutral {
      color: var(--muted);
      border-color: var(--border);
      background: var(--gray-soft);
    }

    .tone-blue {
      color: var(--blue);
      border-color: var(--blue-border);
      background: var(--blue-soft);
    }

    .tone-purple {
      color: var(--purple);
      border-color: var(--purple-border);
      background: var(--purple-soft);
    }

    .main-reason {
      color: var(--heading);
      font-size: 14px;
      font-weight: 850;
      line-height: 1.45;
    }

    .why-list {
      display: grid;
      gap: 7px;
      margin-top: 9px;
    }

    .why-item {
      display: grid;
      gap: 3px;
    }

    .why-detail {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }

    .quick-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }

    .quick-metric {
      border: 1px solid var(--border);
      background: #ffffff;
      border-radius: 14px;
      padding: 10px;
      min-height: 70px;
    }

    .quick-label {
      color: var(--muted);
      font-size: 11px;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }

    .quick-value {
      margin-top: 5px;
      color: var(--heading);
      font-weight: 950;
      font-size: 15px;
      line-height: 1.25;
    }

    .top-action-box {
      border: 1px solid var(--border);
      background: #ffffff;
      border-radius: 16px;
      padding: 12px;
    }

    .top-action-title {
      color: var(--muted);
      font-size: 11px;
      font-weight: 950;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-bottom: 6px;
    }

    .top-action-text {
      color: var(--heading);
      font-size: 13px;
      font-weight: 850;
      line-height: 1.4;
    }

    .card-body {
      padding: 16px;
      display: grid;
      gap: 14px;
    }

    .section-grid {
      display: grid;
      grid-template-columns: 1.25fr 1fr;
      gap: 14px;
    }

    .detail-section {
      border: 1px solid var(--border);
      border-radius: 18px;
      background: var(--card-soft);
      overflow: hidden;
    }

    .section-title {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      padding: 13px 14px;
      border-bottom: 1px solid var(--border);
      background: #ffffff;
    }

    .section-title h3 {
      margin: 0;
      font-size: 14px;
      color: var(--heading);
      font-weight: 950;
      letter-spacing: -0.01em;
    }

    .section-body {
      padding: 13px 14px;
    }

    .story {
      color: var(--text);
      font-size: 13px;
      line-height: 1.55;
      margin-bottom: 12px;
    }

    .risk-math {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 9px;
      margin-bottom: 12px;
    }

    .math-card {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 10px;
    }

    .math-label {
      color: var(--muted);
      font-size: 11px;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }

    .math-value {
      margin-top: 5px;
      color: var(--heading);
      font-size: 20px;
      font-weight: 950;
      font-variant-numeric: tabular-nums;
    }

    .math-note {
      margin-top: 5px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }

    .bucket-table,
    .simple-table {
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      overflow: hidden;
      border: 1px solid var(--border);
      border-radius: 14px;
      background: #ffffff;
      font-size: 12px;
    }

    .bucket-table th,
    .bucket-table td,
    .simple-table th,
    .simple-table td {
      padding: 9px 10px;
      border-bottom: 1px solid var(--border);
      text-align: left;
      vertical-align: top;
    }

    .bucket-table tr:last-child td,
    .simple-table tr:last-child td {
      border-bottom: 0;
    }

    .bucket-table th,
    .simple-table th {
      background: var(--gray-soft);
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      font-weight: 950;
    }

    .num {
      text-align: right !important;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }

    .manager-note {
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }

    .metric-list {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 9px;
    }

    .metric-box {
      border: 1px solid var(--border);
      background: #ffffff;
      border-radius: 14px;
      padding: 10px;
    }

    .metric-label {
      color: var(--muted);
      font-size: 11px;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }

    .metric-value {
      margin-top: 5px;
      color: var(--heading);
      font-weight: 950;
      font-size: 15px;
      font-variant-numeric: tabular-nums;
      word-break: break-word;
    }

    .actions-list {
      display: grid;
      gap: 9px;
    }

    .action-item {
      border: 1px solid var(--border);
      background: #ffffff;
      border-radius: 15px;
      padding: 11px;
    }

    .action-top {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      color: var(--heading);
      font-size: 13px;
      font-weight: 950;
    }

    .action-text {
      margin-top: 7px;
      color: var(--text);
      font-size: 13px;
      line-height: 1.45;
      font-weight: 760;
    }

    .action-evidence {
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }

    .card-footer {
      padding: 13px 16px;
      border-top: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      background: #ffffff;
    }

    .open-link {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      height: 34px;
      padding: 0 13px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: #ffffff;
      color: var(--blue);
      font-size: 12px;
      font-weight: 950;
      white-space: nowrap;
    }

    details.more-details {
      border: 1px solid var(--border);
      border-radius: 18px;
      background: #ffffff;
      overflow: hidden;
    }

    details.more-details summary {
      cursor: pointer;
      list-style: none;
      padding: 13px 14px;
      font-size: 13px;
      font-weight: 950;
      color: var(--blue);
      background: #ffffff;
    }

    details.more-details summary::-webkit-details-marker {
      display: none;
    }

    .more-details-body {
      border-top: 1px solid var(--border);
      padding: 13px 14px;
      background: var(--card-soft);
    }

    .muted {
      color: var(--muted);
    }

    .small {
      font-size: 12px;
    }

    .error {
      color: var(--red);
    }

    .empty-state {
      padding: 48px 16px;
      text-align: center;
      color: var(--muted);
    }

    .empty-title {
      color: var(--heading);
      font-weight: 950;
      font-size: 17px;
      margin-bottom: 6px;
    }

    .footer-bar {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 16px;
      border-top: 1px solid var(--border);
      color: var(--muted);
      font-size: 12px;
      background: #ffffff;
    }

    @media (max-width: 1320px) {
      .summary-grid {
        grid-template-columns: repeat(3, 1fr);
      }

      .filters {
        grid-template-columns: repeat(3, minmax(160px, 1fr));
      }

      .card-head {
        grid-template-columns: 1fr;
      }

      .section-grid {
        grid-template-columns: 1fr;
      }
    }

    @media (max-width: 760px) {
      .page {
        width: min(100% - 20px, 1780px);
        padding-top: 16px;
      }

      .topbar {
        flex-direction: column;
      }

      .top-actions {
        justify-content: flex-start;
      }

      .summary-grid,
      .filters,
      .quick-grid,
      .risk-math,
      .metric-list {
        grid-template-columns: 1fr;
      }

      .footer-bar {
        flex-direction: column;
      }
    }
  </style>
</head>

<body>
  <main class="page">
    <section class="topbar">
      <div>
        <div class="eyebrow"><span class="dot"></span> 30-day cached business view</div>
        <h1>Caretaker Performance Dashboard</h1>
        <div class="sub">
          Manager-ready view for caretaker performance. It separates completed visits, explainable booking/full cases,
          recovered connected follow-ups, and real risky not-done visits.
        </div>
      </div>

      <div class="top-actions">
        <button type="button" class="refresh-btn" onclick="loadRows()">Refresh</button>
      </div>
    </section>

    <section class="summary-grid" id="summaryCards"></section>

    <section class="panel">
      <form id="filters" class="filters">
        <div>
          <label>Username</label>
          <input name="username" placeholder="Search caretaker username" />
        </div>

        <div>
          <label>Phone</label>
          <input name="phone" placeholder="Search phone number" />
        </div>

        <div>
          <label>Risk</label>
          <select name="risk">
            <option value="">Any risk</option>
            <option>High</option>
            <option>Medium</option>
            <option>Low</option>
            <option>No Data</option>
          </select>
        </div>

        <div>
          <label>Search</label>
          <input name="search" placeholder="Search reason, action, email, or caretaker" />
        </div>

        <div>
          <label>Limit</label>
          <input name="limit" type="number" min="1" max="1000" value="100" />
        </div>

        <div>
          <button type="submit" class="apply-btn">Apply</button>
        </div>
      </form>
    </section>

    <section class="panel content-panel">
      <div class="toolbar">
        <div>
          <div class="toolbar-title">Caretaker performance records</div>
          <div class="toolbar-subtitle">Cache-only dashboard. Run the batch job to refresh all 30-day ratings.</div>
        </div>
        <div class="status-chip" id="status">Loading…</div>
      </div>

      <div class="rows-wrap" id="rows">
        <div class="empty-state">
          <div class="empty-title">Loading caretaker performance…</div>
          <div>Fetching cached 30-day caretaker reviews.</div>
        </div>
      </div>

      <div class="footer-bar">
        <span id="footerLeft">-</span>
        <span>Tip: sort by risky site visits to see the most urgent operational gaps first.</span>
      </div>
    </section>
  </main>

<script>
function esc(value) {
  return String(value ?? "").replace(/[&<>'"]/g, ch => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;"
  }[ch]));
}

function toNum(value) {
  const num = Number(value);
  return Number.isFinite(num) ? num : 0;
}

function display(value, fallback = "-") {
  if (value === undefined || value === null || value === "") return fallback;
  return value;
}

function feedbackRatio(received, total) {
  const r = Number(received ?? 0);
  const t = Number(total ?? 0);

  const safeReceived = Number.isFinite(r) ? r : 0;

  if (!Number.isFinite(t) || t <= 0) {
    return `${safeReceived} / total unavailable`;
  }

  return `${safeReceived} / ${t}`;
}

function fmtPct(value) {
  if (value === undefined || value === null || value === "") return "-";
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  return `${num.toFixed(2).replace(/\.00$/, "")}%`;
}

function fmtHours(value) {
  if (value === undefined || value === null || value === "") return "-";

  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);

  if (num >= 24) {
    return `${(num / 24).toFixed(1).replace(/\.0$/, "")} days`;
  }

  return `${num.toFixed(1).replace(/\.0$/, "")} hrs`;
}

function shortDate(value) {
  if (!value) return "-";
  return String(value).replace("T", " ").slice(0, 19);
}

function normalizeRiskClass(value) {
  const risk = String(value || "").trim().toLowerCase();
  if (!risk) return "risk-no-data";
  return "risk-" + risk.replace(/\s+/g, "-");
}

function riskPill(value) {
  const label = value || "No Data";
  return `<span class="pill ${esc(normalizeRiskClass(label))}">${esc(label)}</span>`;
}

function scoreBadge(value, label) {
  const safe = value === undefined || value === null || value === "" ? "-" : value;
  return `<span class="score ${safe === "-" ? "light" : ""}" title="${esc(label || "score")}">${esc(safe)}</span>`;
}

function toneClass(tone) {
  const t = String(tone || "neutral").toLowerCase();
  if (t === "danger") return "danger";
  if (t === "warning") return "warning";
  if (t === "good") return "good";
  return "";
}

function paramsFromForm() {
  const data = new FormData(document.getElementById("filters"));
  const params = new URLSearchParams();

  for (const [key, value] of data.entries()) {
    const clean = String(value).trim();
    if (clean) params.set(key, clean);
  }

  return params;
}

function section(row, key) {
  return (((row || {}).sections || {})[key]) || {};
}

function totals(obj) {
  return ((obj || {}).totals) || {};
}

function riskMath(row) {
  return (section(row, "site_visit_intelligence").risk_math) || {};
}

function renderSummaryCards(cards) {
  const box = document.getElementById("summaryCards");

  if (!Array.isArray(cards) || !cards.length) {
    box.innerHTML = `
      <div class="summary-card">
        <div class="summary-label">Status</div>
        <div class="summary-value">-</div>
        <div class="summary-note">No summary data yet</div>
      </div>`;
    return;
  }

  box.innerHTML = cards.map(card => `
    <div class="summary-card ${esc(toneClass(card.tone))}">
      <div class="summary-label">${esc(card.label || card.key || "-")}</div>
      <div class="summary-value">${esc(display(card.value))}</div>
      ${card.help_text ? `<div class="summary-note">${esc(card.help_text)}</div>` : ""}
    </div>
  `).join("");
}

function whyRiskyHtml(row) {
  const chips = Array.isArray(row.why_risky) ? row.why_risky : [];

  if (!chips.length) {
    return `<div class="muted small">No risk reason available.</div>`;
  }

  return `<div class="why-list">` + chips.slice(0, 4).map(chip => {
    const severity = String(chip.severity || "medium").toLowerCase();
    const cls = severity.includes("high")
      ? "risk-high"
      : severity.includes("medium")
        ? "risk-medium"
        : severity.includes("summary")
          ? "tone-blue"
          : "risk-low";

    return `<div class="why-item">
      <div><span class="pill ${cls}">${esc(chip.label || "-")}</span></div>
      ${chip.detail ? `<div class="why-detail">${esc(chip.detail)}</div>` : ""}
    </div>`;
  }).join("") + `</div>`;
}

function bucketSeverityClass(bucket) {
  const severity = String((bucket || {}).severity || "").toLowerCase();

  if (severity.includes("high") || severity === "risk") return "risk-high";
  if (severity.includes("recover") || severity === "good") return "risk-low";
  if (severity.includes("neutral")) return "tone-blue";
  if (severity.includes("unknown")) return "risk-no-data";
  return "risk-medium";
}


function riskySiteVisitDetailsHtml(row) {
  const rows = (((row.context_tables || {}).risky_site_visits) || []);

  if (!Array.isArray(rows) || !rows.length) {
    return `<div class="manager-note">Risky visit details are not available in cache. Run force refresh for this caretaker.</div>`;
  }

  const tableRows = rows.map((item, index) => {
    const caretakerMissed = toNum(item.missed_calls_from_caretaker_near_visit);
    const customerMissed = toNum(item.missed_calls_from_customer_near_visit);
    const caretakerAttempt = caretakerMissed > 0 ? `Yes (${caretakerMissed})` : "No";
    const customerAttempt = customerMissed > 0 ? `Yes (${customerMissed})` : "No";
    const attemptStatus = customerMissed > 0 && caretakerMissed === 0
      ? "Critical: customer tried, caretaker did not attempt"
      : customerMissed > 0 && caretakerMissed > 0
        ? "Both sides attempted, no connected follow-up"
        : caretakerMissed > 0
          ? "Caretaker attempted, no connected follow-up"
          : "No clear attempt source";
    return `
    <tr>
      <td class="num">${esc(index + 1)}</td>
      <td>${esc(shortDate(item.time))}</td>
      <td>${esc(display(item.lead_id))}</td>
      <td>${esc(display(item.prop_id))}</td>
      <td>${esc(display(item.property))}</td>
      <td>${esc(display(item.status || item.status_code))}</td>
      <td>${esc(caretakerAttempt)}</td>
      <td>${esc(customerAttempt)}</td>
      <td>${esc(attemptStatus)}</td>
      <td>${esc(display(item.manager_reason || item.not_done_reason_candidate))}</td>
    </tr>
  `;
  }).join("");

  return `<details class="more-details" style="margin-bottom:12px">
    <summary>View risky site visit details (${esc(rows.length)})</summary>
    <div class="more-details-body">
      <table class="simple-table">
        <thead>
          <tr>
            <th class="num">#</th>
            <th>Time</th>
            <th>Lead ID</th>
            <th>Property ID</th>
            <th>Property</th>
            <th>Status</th>
            <th>Caretaker attempted?</th>
            <th>Customer attempted?</th>
            <th>Priority</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>${tableRows}</tbody>
      </table>
    </div>
  </details>`;
}


function siteVisitSectionHtml(row) {
  const svi = section(row, "site_visit_intelligence");
  const t = totals(svi);
  const r = riskMath(row);
  const preCall = t.pre_visit_call || {};
  const buckets = Array.isArray(svi.buckets) ? svi.buckets : [];

  const bucketHtml = buckets.map(bucket => `
    <tr>
      <td><span class="pill ${bucketSeverityClass(bucket)}">${esc(bucket.label || bucket.key || "-")}</span></td>
      <td class="num"><b>${esc(bucket.count ?? 0)}</b></td>
      <td>${esc(bucket.manager_interpretation || bucket.meaning || "")}</td>
    </tr>
  `).join("");

  return `<section class="detail-section">
    <div class="section-title">
      <h3>Site Visit Intelligence</h3>
      <span class="pill ${toNum(r.risky_site_visit_rows) > 0 ? "risk-high" : "risk-low"}">
        ${esc(display(r.risky_site_visit_rows, 0))} risky
      </span>
    </div>

    <div class="section-body">
      <div class="story">
        <b>${esc(svi.headline || "Site visit breakdown")}</b><br>
        ${esc(svi.manager_story || "")}
      </div>
      ${toNum(r.risky_site_visit_rows) > 0 ? riskySiteVisitDetailsHtml(row) : ""}

      <div class="risk-math">
        <div class="math-card">
          <div class="math-label">Risky not-done visits</div>
          <div class="math-value">${esc(display(r.risky_site_visit_rows, 0))}</div>
          <div class="math-note">
            ${esc(display(r.missed_call_no_connected_followup, 0))} missed-call/no follow-up
            + ${esc(display(r.no_booking_no_call_activity, 0))} no booking/no call
          </div>
        </div>

        <div class="math-card">
          <div class="math-label">Explainable / recovered</div>
          <div class="math-value">${esc(display(r.explainable_or_recovered_not_done, 0))}</div>
          <div class="math-note">
            ${esc(display(r.booking_full_explainable, 0))} booking/full
            + ${esc(display(r.connected_followup_recovered, 0))} connected follow-up
          </div>
        </div>

        <div class="math-card">
          <div class="math-label">Done visits</div>
          <div class="math-value">${esc(display(t.done_visits, 0))}</div>
          <div class="math-note">Out of ${esc(display(t.total_site_visits, 0))} total visits</div>
        </div>

        <div class="math-card">
          <div class="math-label">Pre-visit call coverage</div>
          <div class="math-value">
            ${esc(display(preCall.done_site_visits_with_pre_call, 0))}/${esc(display(preCall.done_site_visits || t.done_visits, 0))}
          </div>
          <div class="math-note">
            ${esc(fmtPct(preCall.pre_call_coverage_pct))} done visits had same-day pre-call
          </div>
        </div>

        <div class="math-card">
          <div class="math-label">Avg call time</div>
          <div class="math-value">${esc(display(preCall.avg_call_minutes_before_visit))} min</div>
          <div class="math-note">Before visit, using nearest previous connected call</div>
        </div>

        <div class="math-card">
          <div class="math-label">Scheduled not done</div>
          <div class="math-value">${esc(display(t.scheduled_not_done_visits, 0))}</div>
          <div class="math-note">Do not treat all as caretaker failure.</div>
        </div>
      </div>

      

      <table class="bucket-table">
        <thead>
          <tr>
            <th>Bucket</th>
            <th class="num">Count</th>
            <th>Manager interpretation</th>
          </tr>
        </thead>
        <tbody>${bucketHtml}</tbody>
      </table>

      ${svi.manager_note ? `<div class="manager-note">${esc(svi.manager_note)}</div>` : ""}
    </div>
  </section>`;
}

function communicationSectionHtml(row) {
  const comm = section(row, "communication_health");
  const t = totals(comm);

  return `<section class="detail-section">
    <div class="section-title">
      <h3>Communication Health</h3>
      <span class="pill ${toNum(t.missed_rate_pct) >= 30 ? "risk-high" : "risk-low"}">${esc(comm.status || "-")}</span>
    </div>
    <div class="section-body">
      <div class="story"><b>${esc(comm.headline || "Communication")}</b></div>
      <div class="metric-list">
        <div class="metric-box"><div class="metric-label">Total calls</div><div class="metric-value">${esc(display(t.total_calls, 0))}</div></div>
        <div class="metric-box"><div class="metric-label">Connected</div><div class="metric-value">${esc(display(t.connected_calls, 0))}</div></div>
        <div class="metric-box"><div class="metric-label">Missed / zero-duration</div><div class="metric-value">${esc(display(t.missed_or_zero_duration_calls, 0))}</div></div>
        <div class="metric-box"><div class="metric-label">Connect rate</div><div class="metric-value">${esc(fmtPct(t.connect_rate_pct))}</div></div>
        <div class="metric-box"><div class="metric-label">Missed rate</div><div class="metric-value">${esc(fmtPct(t.missed_rate_pct))}</div></div>
        <div class="metric-box"><div class="metric-label">External calls</div><div class="metric-value">${esc(display(t.external_calls, 0))}</div></div>
        <div class="metric-box"><div class="metric-label">Internal calls</div><div class="metric-value">${esc(display(t.internal_calls, 0))}</div></div>
        <div class="metric-box"><div class="metric-label">WhatsApp total</div><div class="metric-value">${esc(display(t.whatsapp_total, 0))}</div></div>
      </div>
      ${comm.manager_note ? `<div class="manager-note">${esc(comm.manager_note)}</div>` : ""}
    </div>
  </section>`;
}

function ticketSectionHtml(row) {
  const ticket = section(row, "ticket_context");
  const t = totals(ticket);

  return `<section class="detail-section">
    <div class="section-title">
      <h3>Ticket Context</h3>
      <span class="pill ${ticket.status === "quality_concern" ? "risk-medium" : "tone-blue"}">${esc(ticket.status || "visible")}</span>
    </div>
    <div class="section-body">
      <div class="story"><b>${esc(ticket.headline || "Assigned-building ticket context")}</b></div>
      <div class="metric-list">
        <div class="metric-box"><div class="metric-label">Visible tickets</div><div class="metric-value">${esc(display(t.visible_tickets, 0))}</div></div>
        <div class="metric-box"><div class="metric-label">Closed</div><div class="metric-value">${esc(display(t.closed_tickets, 0))}</div></div>
        <div class="metric-box"><div class="metric-label">Open</div><div class="metric-value">${esc(display(t.open_tickets, 0))}</div></div>
        <div class="metric-box"><div class="metric-label">Reopened</div><div class="metric-value">${esc(display(t.reopened_tickets, 0))}</div></div>

        <div class="metric-box"><div class="metric-label">Avg rating</div><div class="metric-value">${esc(display(t.avg_ticket_rating))}</div></div>
        <div class="metric-box"><div class="metric-label">Rating sum</div><div class="metric-value">${esc(display(t.ticket_rating_sum))}</div></div>
        <div class="metric-box"><div class="metric-label">Rated tickets</div><div class="metric-value">${esc(display(t.rated_tickets, 0))}</div></div>
        <div class="metric-box"><div class="metric-label">Unrated tickets</div><div class="metric-value">${esc(display(t.unrated_tickets, 0))}</div></div>
        <div class="metric-box"><div class="metric-label">Rating coverage</div><div class="metric-value">${esc(fmtPct(t.ticket_rating_coverage_pct))}</div></div>
        <div class="metric-box"><div class="metric-label">Rating formula</div><div class="metric-value">${esc(display(t.rating_formula))}</div></div>

        <div class="metric-box"><div class="metric-label">Resolved with time</div><div class="metric-value">${esc(display(t.ticket_resolution_count, 0))}</div></div>
        <div class="metric-box"><div class="metric-label">Avg resolution time</div><div class="metric-value">${esc(fmtHours(t.avg_ticket_resolution_hours))}</div></div>
        <div class="metric-box"><div class="metric-label">Median resolution time</div><div class="metric-value">${esc(fmtHours(t.median_ticket_resolution_hours))}</div></div>
        <div class="metric-box"><div class="metric-label">Min resolution time</div><div class="metric-value">${esc(fmtHours(t.min_ticket_resolution_hours))}</div></div>
        <div class="metric-box"><div class="metric-label">Max resolution time</div><div class="metric-value">${esc(fmtHours(t.max_ticket_resolution_hours))}</div></div>
        <div class="metric-box"><div class="metric-label">Resolved within 2h</div><div class="metric-value">${esc(display(t.tickets_resolved_within_2h, 0))}</div></div>
        <div class="metric-box"><div class="metric-label">Resolved 2-24h</div><div class="metric-value">${esc(display(t.tickets_resolved_2_to_24h, 0))}</div></div>
        <div class="metric-box"><div class="metric-label">Resolved 1-2d</div><div class="metric-value">${esc(display(t.tickets_resolved_1_to_2d, 0))}</div></div>
        <div class="metric-box"><div class="metric-label">Resolved after 2d</div><div class="metric-value">${esc(display(t.tickets_resolved_after_2d, 0))}</div></div>
      </div>
      ${ticket.attribution_note ? `<div class="manager-note">${esc(ticket.attribution_note)}</div>` : ""}
    </div>
  </section>`;
}

function propertySectionHtml(row) {
  const prop = section(row, "property_management");
  const t = totals(prop);

  const checkinText = feedbackRatio(
  t.checkin_feedback_received ?? t.checkin_feedback,
  t.checkin_feedback_total
);

  const checkoutText = feedbackRatio(
  t.checkout_feedback_received ?? t.checkout_feedback,
  t.checkout_feedback_total
);

  return `<section class="detail-section">
    <div class="section-title">
      <h3>Property Management</h3>
      <span class="pill tone-purple">${esc(prop.headline || "Property context")}</span>
    </div>
    <div class="section-body">
      <div class="metric-list">
        <div class="metric-box">
          <div class="metric-label">Assigned buildings</div>
          <div class="metric-value">${esc(display(t.assigned_buildings, 0))}</div>
        </div>

        <div class="metric-box">
          <div class="metric-label">Vacant properties</div>
          <div class="metric-value">${esc(display(t.vacant_properties, 0))}</div>
        </div>

        <div class="metric-box">
          <div class="metric-label">Check-in feedback</div>
          <div class="metric-value">${esc(checkinText)}</div>
        </div>

        <div class="metric-box">
          <div class="metric-label">Check-in missing</div>
          <div class="metric-value">${esc(display(t.checkin_feedback_missing, 0))}</div>
        </div>

        <div class="metric-box">
          <div class="metric-label">Check-in coverage</div>
          <div class="metric-value">${esc(fmtPct(t.checkin_feedback_coverage_pct))}</div>
        </div>

        <div class="metric-box">
          <div class="metric-label">Checkout feedback</div>
          <div class="metric-value">${esc(checkoutText)}</div>
        </div>

        <div class="metric-box">
          <div class="metric-label">Checkout missing</div>
          <div class="metric-value">${esc(display(t.checkout_feedback_missing, 0))}</div>
        </div>

        <div class="metric-box">
          <div class="metric-label">Checkout coverage</div>
          <div class="metric-value">${esc(fmtPct(t.checkout_feedback_coverage_pct))}</div>
        </div>

        <div class="metric-box">
          <div class="metric-label">Avg check-in stay rating</div>
          <div class="metric-value">${esc(display(t.avg_checkin_stay_rating))}</div>
        </div>

        <div class="metric-box">
          <div class="metric-label">Avg checkout RMS rating</div>
          <div class="metric-value">${esc(display(t.avg_checkout_rms_rating))}</div>
        </div>

        <div class="metric-box">
          <div class="metric-label">Avg checkout building rating</div>
          <div class="metric-value">${esc(display(t.avg_checkout_building_rating))}</div>
        </div>
      </div>

      ${prop.manager_note ? `<div class="manager-note">${esc(prop.manager_note)}</div>` : ""}
    </div>
  </section>`;
}

function managerActionsHtml(row) {
  const actions = (((row.sections || {}).manager_actions) || row.actions || []);
  const llmActions = (((row.sections || {}).llm_actions) || []);

  const actionItems = Array.isArray(actions) && actions.length
    ? actions.map((action, index) => `
      <div class="action-item">
        <div class="action-top">
          <span>${index + 1}.</span>
          <span>${esc(action.owner_team || action.owner || "Team")}</span>
          ${action.priority_score !== undefined && action.priority_score !== null ? `<span class="pill risk-medium">P${esc(action.priority_score)}</span>` : ""}
        </div>
        <div class="action-text">${esc(action.action || "-")}</div>
        ${action.evidence ? `<div class="action-evidence">${esc(action.evidence)}</div>` : ""}
      </div>
    `).join("")
    : `<div class="muted small">No manager action available.</div>`;

  const llmHtml = Array.isArray(llmActions) && llmActions.length
    ? `<details class="more-details">
        <summary>View raw AI suggestions</summary>
        <div class="more-details-body">
          <div class="actions-list">
            ${llmActions.map((action, index) => `
              <div class="action-item">
                <div class="action-top">
                  <span>${index + 1}.</span>
                  <span>${esc(action.owner_team || action.owner || "Team")}</span>
                  ${action.priority_score !== undefined && action.priority_score !== null ? `<span class="pill risk-medium">P${esc(action.priority_score)}</span>` : ""}
                </div>
                <div class="action-text">${esc(action.action || "-")}</div>
                ${action.evidence ? `<div class="action-evidence">${esc(action.evidence)}</div>` : ""}
              </div>
            `).join("")}
          </div>
        </div>
      </details>`
    : "";

  return `<section class="detail-section">
    <div class="section-title">
      <h3>Manager Actions</h3>
      <span class="pill tone-blue">${esc(actions.length || 0)} actions</span>
    </div>
    <div class="section-body">
      <div class="actions-list">${actionItems}</div>
      <div style="margin-top:10px">${llmHtml}</div>
    </div>
  </section>`;
}
function renderCard(row) {
  const staff = row.staff || {};
  const rating = row.rating || {};
  const main = row.main_table || {};
  const counts = row.counts || {};
  const derived = row.derived || {};
  const r = riskMath(row);
  const topAction = (row.actions || [])[0] || {};

  const ticketTotal = display(counts.tickets_total, 0);
  const ticketRated = display(counts.tickets_rated ?? counts.ticket_rating_count, 0);
  const ticketUnrated = display(counts.tickets_unrated, 0);
  const ticketAvg = display(counts.avg_ticket_rating);
  const ticketAvgResolution = fmtHours(counts.avg_ticket_resolution_hours);
  const ticketSum = counts.ticket_rating_sum;
  const followupRequired = display(counts.missed_calls_requiring_followup ?? counts.followup_required, 0);
  const followedUp = display(counts.followed_up_count, 0);
  const followupRate = fmtPct(counts.followup_rate_pct ?? derived.followup_rate_pct);
  const avgFollowup = fmtHours(counts.avg_followup_hours ?? derived.avg_followup_hours);
  const followupHtml = Number(followupRequired) > 0
    ? `<div class="quick-metric">
          <div class="quick-label">Follow-up</div>
          <div class="quick-value">
            ${esc(followedUp)}/${esc(followupRequired)} recovered ·
            avg ${esc(avgFollowup)} ·
            ${esc(followupRate)}
          </div>
        </div>`
    : "";

  const ticketFormula = (
    ticketSum !== undefined &&
    ticketSum !== null &&
    ticketSum !== "" &&
    Number(ticketRated) > 0 &&
    ticketAvg !== "-"
  )
    ? `${ticketSum} / ${ticketRated} = ${ticketAvg}`
    : "";

  return `<article class="caretaker-card">
    <div class="card-head">
      <div>
        <div class="caretaker-name">${esc(staff.username || main.caretaker || "Unknown caretaker")}</div>
        <div class="caretaker-meta">
          ${staff.email ? `${esc(staff.email)}<br>` : ""}
          ${staff.phone ? `${esc(staff.phone)}<br>` : ""}
          ${esc(staff.team || "Caretaker")} · ${esc(staff.role_scope || "caretaker")}
        </div>

        <div class="score-row">
          ${riskPill(rating.overall_risk)}
          ${scoreBadge(rating.overall_score, "Overall score")}
          <span class="pill tone-blue">Priority ${esc(display(rating.priority_score))}</span>
        </div>
      </div>

      <div>
        <div class="main-reason">${esc(rating.main_reason || main.why_risky || "No main reason available.")}</div>
        ${whyRiskyHtml(row)}
      </div>

      <div class="quick-grid">
        <div class="quick-metric">
          <div class="quick-label">Site visits</div>
          <div class="quick-value">
            ${esc(display(counts.actual_visits, 0))}/${esc(display(counts.visit_records, 0))} done ·
            ${esc(display(r.risky_site_visit_rows, 0))} risky
          </div>
        </div>

        <div class="quick-metric">
          <div class="quick-label">Booking/full</div>
          <div class="quick-value">${esc(display(r.booking_full_explainable, 0))} explainable</div>
        </div>

        <div class="quick-metric">
          <div class="quick-label">Communication</div>
          <div class="quick-value">
            ${esc(fmtPct(derived.connect_rate_pct))} connect ·
            ${esc(fmtPct(derived.missed_rate_pct))} missed
          </div>
        </div>

        ${followupHtml}

        <div class="quick-metric">
          <div class="quick-label">Tickets</div>
          <div class="quick-value">
            ${esc(ticketTotal)} visible ·
            ${esc(ticketRated)} rated ·
            ${esc(ticketUnrated)} unrated ·
            avg rating ${esc(ticketAvg)} ·
            avg resolution ${esc(ticketAvgResolution)}
            ${ticketFormula ? `<div class="small muted" style="margin-top:4px">Formula: ${esc(ticketFormula)}</div>` : ""}
          </div>
        </div>
      </div>

      <div>
        <div class="top-action-box">
          <div class="top-action-title">Top manager action</div>
          <div class="top-action-text">${esc(topAction.action || main.top_action || "No action available.")}</div>
          ${topAction.evidence ? `<div class="action-evidence">${esc(topAction.evidence)}</div>` : ""}
        </div>
      </div>
    </div>

    <div class="card-body">
      <div class="section-grid">
        ${siteVisitSectionHtml(row)}
        ${communicationSectionHtml(row)}
      </div>

      <div class="section-grid">
        ${ticketSectionHtml(row)}
        ${propertySectionHtml(row)}
      </div>

      ${managerActionsHtml(row)}
    </div>

    <div class="card-footer">
      <div class="small muted">
        Rated at: ${esc(shortDate(rating.updated_at || main.rated_at))} ·
        Window: ${esc(display((row.window || {}).days, 30))} days
      </div>
      <div>
        ${row.detail_url ? `<a class="open-link" href="${esc(row.detail_url)}" target="_blank">Open detailed rating</a>` : ""}
      </div>
    </div>
  </article>`;
}
function updateFooter(data) {
  document.getElementById("footerLeft").textContent =
    `${data.count || 0} shown of ${data.total || 0} cached caretaker ratings`;
}

function renderRows(data) {
  const rowsBox = document.getElementById("rows");
  const rows = Array.isArray(data.rows) ? data.rows : [];

  renderSummaryCards(data.summary_cards || []);
  updateFooter(data);

  if (!rows.length) {
    rowsBox.innerHTML = `
      <div class="empty-state">
        <div class="empty-title">No caretaker ratings found</div>
        <div>Run the batch rating job or adjust filters.</div>
      </div>`;
    return;
  }

  rowsBox.innerHTML = rows.map(renderCard).join("");
}

async function loadRows() {
  const status = document.getElementById("status");
  const rowsBox = document.getElementById("rows");

  status.textContent = "Loading…";
  rowsBox.innerHTML = `
    <div class="empty-state">
      <div class="empty-title">Loading caretaker performance…</div>
      <div>Fetching cached 30-day caretaker reviews.</div>
    </div>`;

  try {
    const params = paramsFromForm();
    const url = "/analytics/capabilities/staff/caretaker-performance/dashboard?" + params.toString();

    const res = await fetch(url, { headers: { accept: "application/json" } });
    const data = await res.json();

    if (!res.ok) {
      throw new Error(JSON.stringify(data));
    }

    status.textContent = `${data.count || 0} shown / ${data.total || 0} total`;
    renderRows(data);
  } catch (err) {
    status.textContent = "Failed";
    rowsBox.innerHTML = `
      <div class="empty-state">
        <div class="empty-title error">Failed to load dashboard</div>
        <div class="error">${esc(err.message || err)}</div>
      </div>`;
  }
}

document.getElementById("filters").addEventListener("submit", event => {
  event.preventDefault();
  loadRows();
});

loadRows();
</script>
</body>
</html>
"""

@router.get("/staff/caretaker-performance/dashboard-page", response_class=HTMLResponse)
def caretaker_performance_dashboard_page() -> HTMLResponse:
    return HTMLResponse(CARETAKER_DASHBOARD_HTML)
