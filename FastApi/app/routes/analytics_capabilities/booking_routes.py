from __future__ import annotations

from datetime import date
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.routes.analytics_capabilities.common import (
    DEFAULT_SCHEMA,
    ActiveBookingRecentActivityRequest,
    CustomerBriefRequest,
    _api_error,
    _build_customer_brief_analysis_prompt,
    _build_customer_brief_evidence,
    _encode,
    _with_copy_blocks,
)
from app.services.analytics_engine.capabilities.active_booking_activity_service import ActiveBookingActivityService
from app.services.analytics_engine.capabilities.customer_brief_service import CustomerBriefService
import app.services.analytics_engine.capabilities.review_booking_communication as booking_review


router = APIRouter()


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}, ()):  # keep 0 / False
            return value
    return None


def _pick_from_dicts(dicts: list[dict[str, Any]], keys: list[str]) -> Any:
    for data in dicts:
        if not isinstance(data, dict):
            continue
        for key in keys:
            value = data.get(key)
            if value not in (None, "", [], {}, ()):  # keep 0 / False
                return value
    return None


def _normalize_booking_dashboard_payload(payload: Any) -> Any:
    """Make dashboard rows stable even if service field names differ.

    review_booking_communication.list_booking_dashboard_rows has evolved a few
    times. Some versions return booking fields as status/current_state/property_name;
    others return booking_status/state/unit_name/display_property_name, sometimes
    top-level and sometimes inside summary/booking_summary. The dashboard page
    expects a nested row["booking"] object, so normalize it here before encoding.
    """
    if not isinstance(payload, dict):
        return payload

    rows = payload.get("rows")
    if not isinstance(rows, list):
        return payload

    for row in rows:
        if not isinstance(row, dict):
            continue

        booking = row.get("booking") if isinstance(row.get("booking"), dict) else {}
        rating = row.get("rating") if isinstance(row.get("rating"), dict) else {}
        summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
        booking_summary = row.get("booking_summary") if isinstance(row.get("booking_summary"), dict) else {}
        rating_summary = rating.get("summary") if isinstance(rating.get("summary"), dict) else {}
        rating_booking_summary = rating.get("booking_summary") if isinstance(rating.get("booking_summary"), dict) else {}

        sources = [booking, row, summary, booking_summary, rating_summary, rating_booking_summary]
        normalized_booking = dict(booking)

        normalized_booking["status"] = _first_non_empty(
            normalized_booking.get("status"),
            _pick_from_dicts(sources, ["status", "booking_status", "raw_status"]),
        )
        normalized_booking["current_state"] = _first_non_empty(
            normalized_booking.get("current_state"),
            _pick_from_dicts(sources, ["current_state", "state", "booking_state", "stay_state"]),
        )
        normalized_booking["property_name"] = _first_non_empty(
            normalized_booking.get("property_name"),
            _pick_from_dicts(
                sources,
                [
                    "property_name",
                    "display_property_name",
                    "unit_name",
                    "property_display",
                    "prop_name",
                    "property",
                    "flat_name",
                    "unit",
                ],
            ),
        )
        normalized_booking["building_name"] = _first_non_empty(
            normalized_booking.get("building_name"),
            _pick_from_dicts(sources, ["building_name", "bname", "building"]),
        )
        normalized_booking["building_id"] = _first_non_empty(
            normalized_booking.get("building_id"),
            _pick_from_dicts(sources, ["building_id", "bid"]),
        )
        normalized_booking["travel_from_date"] = _first_non_empty(
            normalized_booking.get("travel_from_date"),
            _pick_from_dicts(sources, ["travel_from_date", "stay_from", "checkin_date", "check_in_date"]),
        )
        normalized_booking["travel_to_date"] = _first_non_empty(
            normalized_booking.get("travel_to_date"),
            _pick_from_dicts(sources, ["travel_to_date", "stay_to", "checkout_date", "check_out_date"]),
        )
        normalized_booking["customer_name"] = _first_non_empty(
            normalized_booking.get("customer_name"),
            _pick_from_dicts(sources, ["customer_name", "traveller_name", "guest_name", "name"]),
        )
        normalized_booking["customer_phone"] = _first_non_empty(
            normalized_booking.get("customer_phone"),
            _pick_from_dicts(sources, ["customer_phone", "traveller_contact_num", "contact_number", "phone"]),
        )

        row["booking"] = normalized_booking
        if not row.get("detail_url"):
            booking_id = row.get("booking_id") or normalized_booking.get("booking_id") or normalized_booking.get("source_id")
            if booking_id not in (None, ""):
                row["detail_url"] = (
                    "/analytics/capabilities/ui?endpoint=%2Fanalytics%2Fcapabilities%2Fbookings%2Freview"
                    f"&booking_id={booking_id}&customer_days=30&llm=true&include_prompt=true"
                )

    return payload


def _effective_runner_timeout(timeout_seconds: int, max_llm_seconds: int, should_run_llm: bool) -> int:
    """Keep max_llm_seconds query-compatible without passing unsupported kwargs."""
    if should_run_llm:
        return int(max_llm_seconds or timeout_seconds or 60)
    return int(timeout_seconds or 45)


@router.post("/bookings/recent-activity")
def active_booking_recent_activity(payload: ActiveBookingRecentActivityRequest, db: Session = Depends(get_db)) -> Any:
    try:
        service = ActiveBookingActivityService(db=db, schema=payload.schema_name)
        return _encode(
            service.build(
                days=payload.days,
                current_only=payload.current_only,
                limit=payload.limit,
                event_limit=payload.event_limit,
                max_events_per_booking=payload.max_events_per_booking,
                max_text=payload.max_text,
                include_timeline=payload.include_timeline,
                ids_only=payload.ids_only,
                include_contact_scans=payload.include_contact_scans,
                debug=payload.debug,
            )
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/bookings/recent-activity")
def active_booking_recent_activity_get(
    days: int = Query(default=3, ge=1, le=365),
    current_only: bool = Query(default=False),
    limit: int = Query(default=500, ge=1, le=5000),
    event_limit: int = Query(default=10000, ge=1, le=100000),
    max_events_per_booking: int = Query(default=5, ge=0, le=50),
    max_text: int = Query(default=180, ge=40, le=2000),
    include_timeline: bool = Query(default=False),
    ids_only: bool = Query(default=True),
    include_contact_scans: bool = Query(default=False),
    debug: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> Any:
    payload = ActiveBookingRecentActivityRequest(
        days=days,
        current_only=current_only,
        limit=limit,
        event_limit=event_limit,
        max_events_per_booking=max_events_per_booking,
        max_text=max_text,
        include_timeline=include_timeline,
        ids_only=ids_only,
        include_contact_scans=include_contact_scans,
        debug=debug,
        schema=DEFAULT_SCHEMA,
    )
    return active_booking_recent_activity(payload, db)


@router.get("/bookings/review/llm-rating")
def booking_llm_rating_get(
    booking_id: int = Query(...),
    customer_days: int = Query(default=30, ge=1, le=365),
    run_llm: bool = Query(default=False, description="Cache-only by default. Set true or use force_refresh/recompute to call LLM."),
    model: str = Query(default="gpt-5-mini"),
    timeout_seconds: int = Query(default=45, ge=5, le=600),
    max_llm_seconds: int = Query(default=60, ge=5, le=900, description="Hard wall timeout for explicit LLM recompute."),
    max_llm_messages: int = Query(default=12, ge=1, le=50),
    max_llm_text_chars: int = Query(default=220, ge=40, le=2000),
    use_cache: bool = Query(default=True, description="Return status='ok' booking cache by booking_id without rebuilding context."),
    force_refresh: bool = Query(default=False, description="Explicitly recompute even when cache status is ok."),
    refresh: bool = Query(default=False, description="Alias for force_refresh=true."),
    recompute: bool = Query(default=False, description="Alias for force_refresh=true."),
    include_context: bool = Query(default=False),
    include_prompt: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> Any:
    try:
        runner = booking_review.BookingCommunicationReviewRunner(db=db, schema=DEFAULT_SCHEMA)
        should_run_llm = bool(run_llm or force_refresh or refresh or recompute)
        effective_timeout_seconds = _effective_runner_timeout(timeout_seconds, max_llm_seconds, should_run_llm)
        result = runner.review_one(
            booking_id=booking_id,
            customer_days=customer_days,
            run_llm=should_run_llm,
            model=model,
            timeout_seconds=effective_timeout_seconds,
            max_llm_messages=max_llm_messages,
            max_llm_text_chars=max_llm_text_chars,
            use_cache=use_cache,
            force_refresh=force_refresh or refresh or recompute,
            include_context=include_context,
            include_prompt=include_prompt,
        )
        result.setdefault("view", "booking_llm_rating")
        result.setdefault("title", "Booking Handling Review")
        if result.get("llm_prompt"):
            _with_copy_blocks(result)
        return _encode(result)
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/bookings/review/llm_rating")
def booking_llm_rating_get_alias(
    booking_id: int = Query(...),
    customer_days: int = Query(default=30, ge=1, le=365),
    run_llm: bool = Query(default=False, description="Cache-only by default. Set true or use force_refresh/recompute to call LLM."),
    model: str = Query(default="gpt-5-mini"),
    timeout_seconds: int = Query(default=45, ge=5, le=600),
    max_llm_seconds: int = Query(default=60, ge=5, le=900, description="Hard wall timeout for explicit LLM recompute."),
    max_llm_messages: int = Query(default=12, ge=1, le=50),
    max_llm_text_chars: int = Query(default=220, ge=40, le=2000),
    use_cache: bool = Query(default=True),
    force_refresh: bool = Query(default=False),
    refresh: bool = Query(default=False),
    recompute: bool = Query(default=False),
    include_context: bool = Query(default=False),
    include_prompt: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> Any:
    return booking_llm_rating_get(
        booking_id=booking_id,
        customer_days=customer_days,
        run_llm=run_llm,
        model=model,
        timeout_seconds=timeout_seconds,
        max_llm_seconds=max_llm_seconds,
        max_llm_messages=max_llm_messages,
        max_llm_text_chars=max_llm_text_chars,
        use_cache=use_cache,
        force_refresh=force_refresh or refresh or recompute,
        refresh=False,
        recompute=False,
        include_context=include_context,
        include_prompt=include_prompt,
        db=db,
    )


@router.get("/bookings/review")
def booking_review_get(
    booking_id: int = Query(...),
    customer_days: int = Query(default=30, ge=1, le=365),
    llm: bool = Query(default=True),
    run_llm: bool = Query(default=False, description="Cache-only by default. Set true or use force_refresh/recompute to call LLM."),
    display_mode: Literal["evidence", "llm", "raw"] = Query(default="evidence"),
    model: str = Query(default="gpt-5-mini"),
    timeout_seconds: int = Query(default=45, ge=5, le=600),
    max_llm_seconds: int = Query(default=60, ge=5, le=900, description="Hard wall timeout for explicit LLM recompute."),
    max_llm_messages: int = Query(default=12, ge=1, le=50),
    max_llm_text_chars: int = Query(default=220, ge=40, le=2000),
    use_cache: bool = Query(default=True),
    force_refresh: bool = Query(default=False),
    refresh: bool = Query(default=False),
    recompute: bool = Query(default=False),
    include_context: bool = Query(default=False),
    include_prompt: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> Any:
    """UI-friendly booking review endpoint.

    When llm=true, this uses the same cache-first flow as
    /bookings/review/llm-rating. Use llm=false for the older customer-brief
    evidence-only view.
    """
    if llm:
        try:
            runner = booking_review.BookingCommunicationReviewRunner(db=db, schema=DEFAULT_SCHEMA)
            should_run_llm = bool(run_llm or force_refresh or refresh or recompute)
            effective_timeout_seconds = _effective_runner_timeout(timeout_seconds, max_llm_seconds, should_run_llm)
            result = runner.review_one(
                booking_id=booking_id,
                customer_days=customer_days,
                run_llm=should_run_llm,
                model=model,
                timeout_seconds=effective_timeout_seconds,
                max_llm_messages=max_llm_messages,
                max_llm_text_chars=max_llm_text_chars,
                use_cache=use_cache,
                force_refresh=force_refresh or refresh or recompute,
                include_context=include_context or display_mode == "llm",
                include_prompt=include_prompt or display_mode in {"llm", "evidence"},
            )
            result.setdefault("view", "booking_llm_rating")
            result.setdefault("title", "Booking Handling Review")
            result.setdefault(
                "input",
                {
                    "booking_id": booking_id,
                    "customer_days": customer_days,
                    "display_mode": display_mode,
                    "source_endpoint": "/bookings/review",
                },
            )
            if result.get("llm_prompt"):
                _with_copy_blocks(result)
            return _encode(result)
        except Exception as exc:
            raise _api_error(exc) from exc

    # Evidence-only fallback uses existing booking-scoped CustomerBriefService.
    payload = CustomerBriefRequest(
        booking_id=booking_id,
        days=customer_days,
        output_format="llm",
        display_mode=display_mode,
        llm=False,
        print_limit=200,
        max_text=max_llm_text_chars,
        max_llm_messages=max_llm_messages,
        max_llm_text_chars=max_llm_text_chars,
        verbose=False,
        schema=DEFAULT_SCHEMA,
    )
    return customer_brief(payload, db)


@router.post("/bookings/review")
def booking_review_post(payload: dict[str, Any], db: Session = Depends(get_db)) -> Any:
    try:
        booking_id = payload.get("booking_id")
        if booking_id in (None, ""):
            raise ValueError("booking_id is required")
        return booking_review_get(
            booking_id=int(booking_id),
            customer_days=int(payload.get("customer_days") or payload.get("days") or 30),
            llm=bool(payload.get("llm", True)),
            run_llm=bool(payload.get("run_llm") or payload.get("force_refresh") or payload.get("refresh") or payload.get("recompute")),
            display_mode=payload.get("display_mode") or "evidence",
            model=payload.get("model") or "gpt-5-mini",
            timeout_seconds=int(payload.get("timeout_seconds") or 45),
            max_llm_seconds=int(payload.get("max_llm_seconds") or payload.get("hard_timeout_seconds") or 60),
            max_llm_messages=int(payload.get("max_llm_messages") or 12),
            max_llm_text_chars=int(payload.get("max_llm_text_chars") or 220),
            use_cache=bool(payload.get("use_cache", True)),
            force_refresh=bool(payload.get("force_refresh") or payload.get("refresh") or payload.get("recompute")),
            include_context=bool(payload.get("include_context", False)),
            include_prompt=bool(payload.get("include_prompt", True)),
            db=db,
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/bookings/review/dashboard")
def booking_dashboard_get(
    booking_id: Optional[int] = Query(default=None),
    status: Optional[str] = Query(default=None, description="Booking status, e.g. Success."),
    current_state: Optional[str] = Query(default=None, description="active_stay, upcoming_active_booking, checkout_due, stay_completed, inactive_non_success."),
    property_name: Optional[str] = Query(default=None),
    building_id: Optional[int] = Query(default=None),
    owner: Optional[str] = Query(default=None, description="Search Sales/Caretaker/Ops/Finance owner refs."),
    risk: Optional[str] = Query(default=None, description="Overall risk: Low, Medium, High."),
    review_status: Optional[str] = Query(default=None, description="Rating cache status: ok, stale, unrated."),
    min_score: Optional[float] = Query(default=None, ge=0, le=10),
    max_score: Optional[float] = Query(default=None, ge=0, le=10),
    min_priority: Optional[float] = Query(default=None, ge=0, le=10),
    max_priority: Optional[float] = Query(default=None, ge=0, le=10),
    stay_from: Optional[date] = Query(default=None),
    stay_to: Optional[date] = Query(default=None),
    search: Optional[str] = Query(default=None, description="Free-text search across booking, customer, property, owner, and reason."),
    only_rated: bool = Query(default=True, description="true = dashboard of rated bookings; false includes unrated bookings."),
    include_actions: bool = Query(default=True),
    include_stakeholders: bool = Query(default=True),
    sort_by: Literal[
        "booking_id", "status", "current_state", "property_name", "building_id", "score", "overall_score",
        "priority", "work_priority_score", "overall_priority_score","ops_score", "sales_score", "finance_score", "caretaker_score",
        "desk_score", "field_score", "asset_score", "risk", "updated_at", "rated_at", "travel_from_date",
        "travel_to_date", "booking_datetime", "open_tickets", "recent_activity",
    ] = Query(default="priority"),
    sort_dir: Literal["asc", "desc"] = Query(default="desc"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = booking_review.list_booking_dashboard_rows(
            db=db,
            schema=DEFAULT_SCHEMA,
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
            only_rated=only_rated,
            include_actions=include_actions,
            include_stakeholders=include_stakeholders,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
        return _encode(_normalize_booking_dashboard_payload(result))
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/bookings/review/action-queue")
def booking_action_queue_get(
    slice_type: Optional[Literal["ops_manager", "finance_manager", "caretaker", "building", "property"]] = Query(default=None),
    slice_key: Optional[str] = Query(default=None),
    owner_team: Optional[str] = Query(default=None),
    min_priority: Optional[float] = Query(default=None, ge=0, le=10),
    include_no_action: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = booking_review.list_booking_action_queue(
            db=db,
            schema=DEFAULT_SCHEMA,
            slice_type=slice_type,
            slice_key=slice_key,
            owner_team=owner_team,
            min_priority=min_priority,
            include_no_action=include_no_action,
            limit=limit,
            offset=offset,
        )
        return _encode(result)
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/bookings/review/building-summary")
def booking_building_summary_get(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = booking_review.list_booking_building_summary(
            db=db,
            schema=DEFAULT_SCHEMA,
            limit=limit,
            offset=offset,
        )
        return _encode(result)
    except Exception as exc:
        raise _api_error(exc) from exc


BOOKING_DASHBOARD_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Booking Handling Dashboard</title>
  <style>
    :root { color-scheme: light; --bg:#f7f8fa; --card:#fff; --border:#dfe3ea; --text:#172033; --muted:#667085; --bad:#b42318; --mid:#b54708; --ok:#067647; }
    body { margin:0; font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:var(--bg); color:var(--text); }
    header { padding:22px 28px 12px; }
    h1 { margin:0 0 6px; font-size:24px; }
    .sub { color:var(--muted); font-size:13px; }
    .panel { margin:12px 24px; background:var(--card); border:1px solid var(--border); border-radius:14px; box-shadow:0 1px 2px rgba(16,24,40,.05); }
    .filters { display:grid; grid-template-columns: repeat(8, minmax(120px, 1fr)); gap:10px; padding:14px; align-items:end; }
    label { display:block; font-size:12px; color:var(--muted); margin-bottom:4px; }
    input, select, button { width:100%; box-sizing:border-box; border:1px solid var(--border); border-radius:9px; padding:8px 10px; background:#fff; font-size:13px; }
    button { cursor:pointer; background:#172033; color:#fff; border-color:#172033; font-weight:600; }
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
    .action { max-width:360px; }
    .teams { max-width:260px; }
    .nowrap { white-space:nowrap; }
    .statusbar { padding:10px 14px; display:flex; justify-content:space-between; color:var(--muted); font-size:13px; border-top:1px solid var(--border); }
    .error { color:var(--bad); }
    @media (max-width: 1200px) { .filters { grid-template-columns: repeat(3, minmax(120px, 1fr)); } }
    .custom-tooltip { position: absolute; background: #172033; color: #fff; padding: 8px 12px; border-radius: 8px; font-size: 12px; max-width: 350px; white-space: normal; z-index: 1000; box-shadow: 0 2px 8px rgba(0,0,0,0.2); pointer-events: none; }
  </style>
</head>
<body>
  <header>
    <h1>Booking Handling Dashboard</h1>
    <div class="sub">Compact booking details + cached booking_communication_review score. This page does not call the LLM per row.</div>
  </header>

  <section class="panel">
    <form id="filters" class="filters">
      <div><label>Booking ID</label><input name="booking_id" placeholder="57756" /></div>
      <div><label>Status</label><input name="status" placeholder="Success" /></div>
      <div><label>State</label><select name="current_state"><option value="">Any</option><option>active_stay</option><option>checkout_due</option><option>upcoming_active_booking</option><option>stay_completed</option><option>inactive_non_success</option></select></div>
      <div><label>Property</label><input name="property_name" placeholder="unit/building" /></div>
      <div><label>Building ID</label><input name="building_id" type="number" /></div>
      <div><label>Owner/team</label><input name="owner" placeholder="sales/caretaker/ops" /></div>
      <div><label>Risk</label><select name="risk"><option value="">Any</option><option>High</option><option>Medium</option><option>Low</option></select></div>
      <div><label>Min score</label><input name="min_score" type="number" min="0" max="10" step="1" /></div>
      <div><label>Sort</label><select name="sort_by"><option value="priority">Work priority</option><option value="score">Overall score</option><option value="booking_id">Booking ID</option><option value="ops_score">Ops Rating</option><option value="sales_score">Sales Rating</option><option value="finance_score">Finance Rating</option><option value="caretaker_score">Caretaker Rating</option><option value="travel_from_date">Stay start</option><option value="travel_to_date">Stay end</option><option value="updated_at">Rated at</option><option value="open_tickets">Open tickets</option></select></div>
      <div><label>Direction</label><select name="sort_dir"><option value="desc">Desc</option><option value="asc">Asc</option></select></div>
      <div><label>Search</label><input name="search" placeholder="reason/customer/property" /></div>
      <div><label>Rated only</label><select name="only_rated"><option value="true">Yes</option><option value="false">No</option></select></div>
      <div><label>Limit</label><input name="limit" type="number" min="1" max="1000" value="100" /></div>
      <div><button type="submit">Apply filters</button></div>
    </form>
  </section>

  <section class="panel">
    <div style="overflow:auto; max-height:72vh;">
      <table>
        <thead>
          <tr>
            <th>Booking</th><th>Status</th><th>State</th><th>Property</th><th>Stay</th><th class="teams">Stakeholders</th>
            <th class="num">Score</th><th class="num">Priority</th><th class="num">Ops Rating</th><th class="num">Sales rating</th><th class="num">Finance rating</th><th class="num">Caretaker rating</th>
            <th class="num">Open tickets</th><th>Top action</th><th>Rated at</th><th>Links</th><th>Re-rate</th>
          </tr>
        </thead>
        <tbody id="rows"><tr><td colspan="18" class="muted">Loading…</td></tr></tbody>
      </table>
    </div>
    <div class="statusbar"><span id="status">-</span><span class="small">Tip: priority desc shows same-day action risk; score asc shows weak handling.</span></div>
  </section>

<script>
function esc(value) { return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch])); }
function shortDate(value) { if (!value) return '-'; return String(value).replace('T', ' ').slice(0, 19); }
function day(value) { if (!value) return '-'; return String(value).slice(0, 10); }
function firstValue(objects, keys) {
  for (const obj of objects) {
    if (!obj || typeof obj !== 'object') continue;
    for (const key of keys) {
      const value = obj[key];
      if (value !== undefined && value !== null && value !== '' && !(Array.isArray(value) && !value.length)) return value;
    }
  }
  return '';
}
function bookingValue(row, keys) {
  const rating = row.rating || {};
  return firstValue([row.booking || {}, row, row.summary || {}, row.booking_summary || {}, rating.summary || {}, rating.booking_summary || {}], keys);
}
function paramsFromForm() { const data = new FormData(document.getElementById('filters')); const params = new URLSearchParams(); for (const [key, value] of data.entries()) { if (String(value).trim() !== '') params.set(key, String(value).trim()); } return params; }
function teamSummary(row){const s=row.stakeholders||{},parts=[];if(s.ops_manager)parts.push('Ops: '+s.ops_manager);if(s.sales_owner)parts.push('Sales: '+s.sales_owner);if(s.finance_owner)parts.push('Finance: '+s.finance_owner);if(s.caretaker)parts.push('Caretaker: '+s.caretaker);return parts.length?parts.map(esc).join('<br>'):'<span class="muted">-</span>';}
function stakeholderScore(row, team) { const scores = row.stakeholders?.scorecard || [];const wanted = String(team || '').trim().toLowerCase();const found = scores.find(s => {const name = String(s.stakeholder_team || s.team || '').trim().toLowerCase();return name === wanted;});return found?.score ?? '-';}
function priorityDisplay(score){var n=score==null||score===''?NaN:Number(score);if(isNaN(n))return '-';var label=n>=8?'High':n>=4?'Medium':'Low';return n+' ('+label+')';}
function actionLabel(priorityScore, action, ownerTeam, evidence, errorMsg, warnMsg, actionRows) {
  var n = priorityScore == null || priorityScore === '' ? NaN : Number(priorityScore);
  function span(label, color, weight, tooltipHtml) {
    var tmp = document.createElement('div');
    var el = document.createElement('span');
    el.style.cssText = 'cursor:help;color:'+color+';font-weight:'+weight+';';
    el.innerHTML = label;
    el.dataset.tooltip = tooltipHtml;
    el.setAttribute('onmouseenter','showTooltipFromEl(this,event)');
    el.setAttribute('onmouseleave','hideTooltip()');
    tmp.appendChild(el);
    return tmp.innerHTML;
  }
  if (errorMsg) {
    var errHtml = '<b style="color:#b42318;">❌ Review error</b><div style="margin-top:4px;">'+esc(errorMsg)+'</div>';
    return span('❌ Review error','#b42318','500',errHtml);
  }
  if (warnMsg) {
    var warnHtml = '<b style="color:#6b7280;">⚠️ Warning</b><div style="margin-top:4px;">'+esc(warnMsg)+'</div>';
    return span('⚠️ '+esc(warnMsg),'#6b7280','500',warnHtml);
  }
  var team = esc(ownerTeam || '-');
  var reason = esc(evidence || action || '-');
  var normalHtml =
    '<b>Team:</b> ' + team +
    '<br><b>Reason:</b> ' + reason;
  if (!isNaN(n) && n >= 8) return span(' Immediate action req.','#b42318','600',normalHtml);
  if (!isNaN(n) && n >= 4) return span(' Needs attention','#b54708','600',normalHtml);
  return span('No action needed','#067647','400',normalHtml);
}
let tooltipDiv=null;function showTooltip(event,html){if(!tooltipDiv){tooltipDiv=document.createElement('div');tooltipDiv.className='custom-tooltip';document.body.appendChild(tooltipDiv);}tooltipDiv.innerHTML=html;tooltipDiv.style.display='block';tooltipDiv.style.left=(event.pageX+10)+'px';tooltipDiv.style.top=(event.pageY+10)+'px';}function showTooltipFromEl(el,event){showTooltip(event,el.dataset.tooltip||'');}
function buildActionRowsHtml(actionRows){if(!actionRows||!actionRows.length)return '';var rows=actionRows.map(function(r){var pri=r.priority_score!=null?'<span style="font-weight:600;color:'+(r.priority_score>=8?'#b42318':r.priority_score>=4?'#b54708':'#067647')+';">'+esc(r.priority_score)+'/10</span>':'';var owner=r.owner_team?'<span style="font-weight:600;">'+esc(r.owner_team)+'</span>':'';var action=r.action?esc(r.action):'';var evidence=r.evidence?'<div style="color:#6b7280;font-size:11px;margin-top:2px;">'+esc(r.evidence)+'</div>':'';return '<div style="padding:5px 0;border-bottom:1px solid #e5e7eb;">'+ [pri,owner,action].filter(Boolean).join(' &middot; ')+evidence+'</div>';});return '<div style="font-size:12px;font-weight:600;color:#374151;margin-bottom:4px;">Immediate actions</div>'+rows.join('');}
function hideTooltip(){if(tooltipDiv)tooltipDiv.style.display='none';}
function renderRow(row) {
  const b = row.booking || {}, r = row.rating || {}, c = row.counts || {}, a = row.top_action || {};
  const statusValue = bookingValue(row, ['status', 'booking_status', 'raw_status']) || '-';
  const stateValue = bookingValue(row, ['current_state', 'state', 'booking_state', 'stay_state']) || '-';
  const propertyValue = bookingValue(row, ['property_name', 'display_property_name', 'unit_name', 'property_display', 'prop_name', 'property', 'flat_name', 'unit']) || '-';
  const buildingValue = bookingValue(row, ['building_name', 'bname', 'building', 'building_id', 'bid']);
  const stayFromValue = bookingValue(row, ['travel_from_date', 'stay_from', 'checkin_date', 'check_in_date']);
  const stayToValue = bookingValue(row, ['travel_to_date', 'stay_to', 'checkout_date', 'check_out_date']);
  const customerValue = bookingValue(row, ['customer_name', 'traveller_name', 'guest_name', 'customer_phone', 'traveller_contact_num', 'contact_number', 'phone']);
  const errorMsg = (r.status && r.status !== 'ok' && r.status !== 'unrated' && r.status !== 'stale') ? (r.error || 'LLM review failed') : (a.error || null);
  const warnMsg = !errorMsg ? ((!r.status || r.status === 'unrated') ? 'Not yet reviewed' : (r.overall_score == null && r.work_priority_score == null && r.overall_priority_score == null) ? 'LLM returned no scores — review may be incomplete' : null) : null;
  const briefUrl = `/analytics/capabilities/ui?endpoint=%2Fanalytics%2Fcapabilities%2Fcustomer-brief&booking_id=${encodeURIComponent(row.booking_id)}&days=30&llm=true&display_mode=evidence`;
  const rmsUrl = `https://www.rentmystay.com/RBack/invoice_details/${encodeURIComponent(row.booking_id)}`;
  return `<tr data-booking-id="${esc(row.booking_id)}">
    <td class="nowrap"><b>${esc(row.booking_id)}</b><div class="small muted">${esc(customerValue)}</div></td>
    <td>${esc(statusValue)}</td>
    <td>${esc(stateValue)}</td>
    <td>${esc(propertyValue)}<div class="small muted">${esc(buildingValue)}</div></td>
    <td class="nowrap">${esc(day(stayFromValue))}<br><span class="muted">to</span> ${esc(day(stayToValue))}</td>
    <td class="teams small">${teamSummary(row)}</td>
    <td class="num"><b>${esc(r.overall_score ?? '-')}</b></td>
    <td class="num">${priorityDisplay(r.work_priority_score ?? r.overall_priority_score)}</td>
    <td class="num">${esc(stakeholderScore(row, 'ops'))}</td>
    <td class="num">${esc(stakeholderScore(row, 'sales'))}</td>
    <td class="num">${esc(stakeholderScore(row, 'finance'))}</td>
    <td class="num">${esc(stakeholderScore(row, 'caretaker'))}</td>
    <td class="num">${esc(c.open_tickets ?? '-')}</td>
    <td class="action">${actionLabel(a.priority_score ?? r.work_priority_score ?? r.overall_priority_score, a.action || r.main_reason, a.owner_team, a.evidence, errorMsg, warnMsg, r.action_rows || [])}</td>
    <td class="small nowrap">${esc(shortDate(r.updated_at))}</td>
    <td class="nowrap">
        <a href="${esc(row.detail_url || '#')}" target="_blank" style="display:block;margin-bottom:4px;">Review</a>
        <a href="${esc(briefUrl)}" target="_blank" style="display:block;">Brief</a>
        <a href="${esc(rmsUrl)}" target="_blank" style="display:block;">RMS</a>
        <a href="/analytics/capabilities/bookings/review/ops-desk-page" target="_blank" rel="noopener noreferrer" style="display:block;margin-top:4px;">Ops/desk</a>
    </td>
    <td class="nowrap">
        <button onclick="rereview(${esc(row.booking_id)},this)" style="width:auto;padding:4px 10px;font-size:12px;font-weight:500;background:#fff;color:#172033;border:1px solid var(--border);border-radius:7px;cursor:pointer;" title="Force-refresh the LLM review for this booking">Refresh</button>
    </td>
  </tr>`;
}
async function rereview(bookingId, btn) {
  if (btn.dataset.busy === '1') return;
  btn.dataset.busy = '1';
  const orig = btn.textContent;
  btn.textContent = 'Reviewing…';
  btn.style.opacity = '0.6';
  btn.style.cursor = 'not-allowed';
  try {
    const url = `llm-rating?booking_id=${encodeURIComponent(bookingId)}&force_refresh=true&run_llm=true&customer_days=30&max_llm_messages=12&max_llm_text_chars=220&max_llm_seconds=180`;
    const res = await fetch(url);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data?.detail || data?.error || `HTTP ${res.status}`);
    const rowRes = await fetch(`dashboard?booking_id=${encodeURIComponent(bookingId)}`);
    const rowData = await rowRes.json().catch(() => ({}));
    const updatedRow = (rowData.rows || []).find(r => String(r.booking_id) === String(bookingId));
    if (updatedRow) {
      const tr = document.querySelector(`tr[data-booking-id="${bookingId}"]`);
      if (tr) {
        const tmp = document.createElement('tbody');
        tmp.innerHTML = renderRow(updatedRow);
        const newTr = tmp.firstElementChild;
        tr.replaceWith(newTr);
        const newBtn = newTr.querySelector('button');
        if (newBtn) {
          newBtn.textContent = '✓ Done';
          newBtn.style.color = 'var(--ok)';
          newBtn.dataset.busy = '1';
          setTimeout(() => { newBtn.textContent = orig; newBtn.style.color = ''; newBtn.dataset.busy = ''; }, 1500);
        }
        return;
      }
    }
    btn.textContent = '✓ Done';
    btn.style.color = 'var(--ok)';
    btn.style.opacity = '1';
    setTimeout(() => { btn.textContent = orig; btn.style.color = ''; btn.dataset.busy = ''; btn.style.cursor = ''; }, 1500);
  } catch (err) {
    btn.textContent = '✗ Error';
    btn.style.color = 'var(--bad)';
    btn.style.opacity = '1';
    btn.title = String(err);
    setTimeout(() => { btn.textContent = orig; btn.style.color = ''; btn.title = ''; btn.dataset.busy = ''; btn.style.cursor = ''; }, 4000);
  }
}
async function loadRows() {
  const tbody = document.getElementById('rows'); const status = document.getElementById('status');
  tbody.innerHTML = '<tr><td colspan="17" class="muted">Loading…</td></tr>';
  try {
    const res = await fetch('dashboard?' + paramsFromForm().toString());
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));
    const rows = data.rows || [];
    status.textContent = `${rows.length} shown of ${data.total || 0} bookings`;
    if (!rows.length) { tbody.innerHTML = '<tr><td colspan="17" class="muted">No matching bookings.</td></tr>'; return; }
    tbody.innerHTML = rows.map(renderRow).join('');
  } catch (err) {
    status.innerHTML = '<span class="error">Failed to load dashboard</span>';
    tbody.innerHTML = `<tr><td colspan="17" class="error">${esc(err.message || err)}</td></tr>`;
  }
}
document.getElementById('filters').addEventListener('submit', event => { event.preventDefault(); loadRows(); });
loadRows();
</script>
</body>
</html>
"""

OPS_DESK_DASHBOARD_HTML  = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Ops Desk Dashboard</title>
  <style>
    :root { color-scheme: light; --bg:#f7f8fa; --card:#fff; --border:#dfe3ea; --text:#172033; --muted:#667085; --bad:#b42318; --mid:#b54708; --ok:#067647; }
    body { margin:0; font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:var(--bg); color:var(--text); }
    header { padding:22px 28px 12px; }
    h1 { margin:0 0 6px; font-size:24px; }
    .sub { color:var(--muted); font-size:13px; }
    .panel { margin:12px 24px; background:var(--card); border:1px solid var(--border); border-radius:14px; box-shadow:0 1px 2px rgba(16,24,40,.05); }
    .filters { display:grid; grid-template-columns: repeat(8, minmax(120px, 1fr)); gap:10px; padding:14px; align-items:end; }
    label { display:block; font-size:12px; color:var(--muted); margin-bottom:4px; }
    input, select, button { width:100%; box-sizing:border-box; border:1px solid var(--border); border-radius:9px; padding:8px 10px; background:#fff; font-size:13px; }
    button { cursor:pointer; background:#172033; color:#fff; border-color:#172033; font-weight:600; }
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
    .action { max-width:360px; }
    .teams { max-width:260px; }
    .nowrap { white-space:nowrap; }
    .statusbar { padding:10px 14px; display:flex; justify-content:space-between; color:var(--muted); font-size:13px; border-top:1px solid var(--border); }
    .error { color:var(--bad); }
    @media (max-width: 1200px) { .filters { grid-template-columns: repeat(3, minmax(120px, 1fr)); } }
    .custom-tooltip { position: absolute; background: #172033; color: #fff; padding: 8px 12px; border-radius: 8px; font-size: 12px; max-width: 350px; white-space: normal; z-index: 1000; box-shadow: 0 2px 8px rgba(0,0,0,0.2); pointer-events: none; }
  </style>
</head>
<body>
  <header>
    <h1>Ops Desk Dashboard</h1>
    <div class="sub">Ops-focused booking action dashboard using cached booking communication reviews.</div>
  </header>

  <section class="panel">
    <form id="filters" class="filters">
      <div><label>Booking ID</label><input name="booking_id" placeholder="57756" /></div>
      <div><label>Status</label><input name="status" placeholder="Success" /></div>
      <div><label>State</label><select name="current_state"><option value="">Any</option><option>active_stay</option><option>checkout_due</option><option>upcoming_active_booking</option><option>stay_completed</option><option>inactive_non_success</option></select></div>
      <div><label>Property</label><input name="property_name" placeholder="unit/building" /></div>
      <div><label>Building ID</label><input name="building_id" type="number" /></div>
      <div><label>Owner/team</label><input name="owner" placeholder="sales/caretaker/ops" /></div>
      <div><label>Risk</label><select name="risk"><option value="">Any</option><option>High</option><option>Medium</option><option>Low</option></select></div>
      <div><label>Min score</label><input name="min_score" type="number" min="0" max="10" step="1" /></div>
      <div><label>Sort</label><select name="sort_by"><option value="priority">Work priority</option><option value="score">Overall score</option><option value="booking_id">Booking ID</option><option value="desk_score">Desk Rating</option><option value="field_score">Field Rating</option><option value="asset_score">Asset Rating</option><option value="travel_from_date">Stay start</option><option value="travel_to_date">Stay end</option><option value="updated_at">Rated at</option><option value="open_tickets">Open tickets</option></select></div>
      <div><label>Direction</label><select name="sort_dir"><option value="desc">Desc</option><option value="asc">Asc</option></select></div>
      <div><label>Search</label><input name="search" placeholder="reason/customer/property" /></div>
      <div><label>Rated only</label><select name="only_rated"><option value="true">Yes</option><option value="false">No</option></select></div>
      <div><label>Limit</label><input name="limit" type="number" min="1" max="1000" value="100" /></div>
      <div><button type="submit">Apply filters</button></div>
    </form>
  </section>

  <section class="panel">
    <div style="overflow:auto; max-height:72vh;">
      <table>
        <thead>
          <tr>
            <th>Booking</th><th>Status</th><th>State</th><th>Property</th><th>Stay</th><th class="teams">Stakeholders</th>
            <th class="num">Score</th> <th class="num">Desk rating</th><th class="num">Desk priority</th><th class="num">Field rating</th><th class="num">Field priority</th><th class="num">Asset rating</th><th class="num">Asset priority</th>
            <th class="num">Open tickets</th><th>Top action</th><th>Rated at</th><th>Links</th><th>Re-rate</th>
          </tr>
        </thead>
        <tbody id="rows"><tr><td colspan="18" class="muted">Loading…</td></tr></tbody>
      </table>
    </div>
    <div class="statusbar"><span id="status">-</span><span class="small">Tip: priority desc shows same-day action risk; score asc shows weak handling.</span></div>
  </section>

<script>
function esc(value) { return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch])); }
function shortDate(value) { if (!value) return '-'; return String(value).replace('T', ' ').slice(0, 19); }
function day(value) { if (!value) return '-'; return String(value).slice(0, 10); }
function firstValue(objects, keys) {
  for (const obj of objects) {
    if (!obj || typeof obj !== 'object') continue;
    for (const key of keys) {
      const value = obj[key];
      if (value !== undefined && value !== null && value !== '' && !(Array.isArray(value) && !value.length)) return value;
    }
  }
  return '';
}
function bookingValue(row, keys) {
  const rating = row.rating || {};
  return firstValue([row.booking || {}, row, row.summary || {}, row.booking_summary || {}, rating.summary || {}, rating.booking_summary || {}], keys);
}
function paramsFromForm() { const data = new FormData(document.getElementById('filters')); const params = new URLSearchParams(); for (const [key, value] of data.entries()) { if (String(value).trim() !== '') params.set(key, String(value).trim()); } return params; }
function teamSummary(row){const s=row.stakeholders||{},parts=[];if(s.ops_manager)parts.push('Ops Manager: '+s.ops_manager);if(s.ops_owner)parts.push('Desk: '+s.ops_owner);if(s.caretaker)parts.push('Caretaker: '+s.caretaker);return parts.length?parts.map(esc).join('<br>'):'<span class="muted">-</span>';}
function stakeholderRow(row, team) {const scores = row.stakeholders?.scorecard || [];const wanted = String(team || '').trim().toLowerCase();
  return scores.find(s => {const name = String(s.stakeholder_team || s.team || '').trim().toLowerCase();return name === wanted;}) || {};}
function stakeholderScore(row, team) {const found = stakeholderRow(row, team);return found.score ?? '-';}
function stakeholderPriority(row, team) {const found = stakeholderRow(row, team);return found.priority_score ?? '-';}
function opsSubteamRow(row, subteam) {const ops = stakeholderRow(row, 'ops');const subteams = ops.subteam_scores || ops.subteams || [];const wanted = String(subteam || '').trim().toLowerCase();
  return subteams.find(s => {const name = String(s.subteam || s.ops_subteam || s.team || '').trim().toLowerCase();return name === wanted;}) || {};}
function opsSubteamScore(row, subteam) {const found = opsSubteamRow(row, subteam);return found.score ?? '-';}
function opsSubteamPriority(row, subteam) {const found = opsSubteamRow(row, subteam);return found.priority_score ?? '-';}
function priorityDisplay(score){var n=score==null||score===''?NaN:Number(score);if(isNaN(n))return '-';var label=n>=8?'High':n>=4?'Medium':'Low';return n+' ('+label+')';}
function actionLabel(priorityScore, action, ownerTeam, evidence, errorMsg, warnMsg, actionRows) {
  var n = priorityScore == null || priorityScore === '' ? NaN : Number(priorityScore);
  function htmlEsc(value) {
    return String(value ?? '').replace(/[&<>"']/g, function(ch) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[ch];
    });
  }
  function span(label, color, weight, tooltipHtml) {
    var tmp = document.createElement('div');
    var el = document.createElement('span');
    el.style.cssText = 'cursor:help;color:'+color+';font-weight:'+weight+';';
    el.innerHTML = htmlEsc(label);
    el.dataset.tooltip = tooltipHtml;
    el.setAttribute('onmouseenter','showTooltipFromEl(this,event)');
    el.setAttribute('onmouseleave','hideTooltip()');
    tmp.appendChild(el);
    return tmp.innerHTML;
  }
  if (errorMsg) {
    var errHtml = '<b style="color:#b42318;">❌ Review error</b><div style="margin-top:4px;">'+htmlEsc(errorMsg)+'</div>';
    return span('❌ Review error','#b42318','500',errHtml);
  }
  if (warnMsg) {
    var warnHtml = '<b style="color:#6b7280;">⚠️ Warning</b><div style="margin-top:4px;">'+htmlEsc(warnMsg)+'</div>';
    return span('⚠️ '+warnMsg,'#6b7280','500',warnHtml);
  }
  var team = htmlEsc(ownerTeam || '-');
  var reason = htmlEsc(evidence || action || '-');
  var normalHtml =
    '<b>Team:</b> ' + team +
    '<br><b>Reason:</b> ' + reason;
  if (!isNaN(n) && n >= 8) return span('Immediate action req.','#b42318','600',normalHtml);
  if (!isNaN(n) && n >= 4) return span('Needs attention','#b54708','600',normalHtml);
  return span('No action needed','#067647','400',normalHtml);
}
let tooltipDiv=null;function showTooltip(event,html){if(!tooltipDiv){tooltipDiv=document.createElement('div');tooltipDiv.className='custom-tooltip';document.body.appendChild(tooltipDiv);}tooltipDiv.innerHTML=html;tooltipDiv.style.display='block';tooltipDiv.style.left=(event.pageX+10)+'px';tooltipDiv.style.top=(event.pageY+10)+'px';}function showTooltipFromEl(el,event){showTooltip(event,el.dataset.tooltip||'');}
function buildActionRowsHtml(actionRows){if(!actionRows||!actionRows.length)return '';var rows=actionRows.map(function(r){var pri=r.priority_score!=null?'<span style="font-weight:600;color:'+(r.priority_score>=8?'#b42318':r.priority_score>=4?'#b54708':'#067647')+';">'+esc(r.priority_score)+'/10</span>':'';var owner=r.owner_team?'<span style="font-weight:600;">'+esc(r.owner_team)+'</span>':'';var action=r.action?esc(r.action):'';var evidence=r.evidence?'<div style="color:#6b7280;font-size:11px;margin-top:2px;">'+esc(r.evidence)+'</div>':'';return '<div style="padding:5px 0;border-bottom:1px solid #e5e7eb;">'+ [pri,owner,action].filter(Boolean).join(' &middot; ')+evidence+'</div>';});return '<div style="font-size:12px;font-weight:600;color:#374151;margin-bottom:4px;">Immediate actions</div>'+rows.join('');}
function hideTooltip(){if(tooltipDiv)tooltipDiv.style.display='none';}
function renderRow(row) {
  const b = row.booking || {}, r = row.rating || {}, c = row.counts || {}, a = row.ops_desk_top_action || {};
  const statusValue = bookingValue(row, ['status', 'booking_status', 'raw_status']) || '-';
  const stateValue = bookingValue(row, ['current_state', 'state', 'booking_state', 'stay_state']) || '-';
  const propertyValue = bookingValue(row, ['property_name', 'display_property_name', 'unit_name', 'property_display', 'prop_name', 'property', 'flat_name', 'unit']) || '-';
  const buildingValue = bookingValue(row, ['building_name', 'bname', 'building', 'building_id', 'bid']);
  const stayFromValue = bookingValue(row, ['travel_from_date', 'stay_from', 'checkin_date', 'check_in_date']);
  const stayToValue = bookingValue(row, ['travel_to_date', 'stay_to', 'checkout_date', 'check_out_date']);
  const customerValue = bookingValue(row, ['customer_name', 'traveller_name', 'guest_name', 'customer_phone', 'traveller_contact_num', 'contact_number', 'phone']);
  const errorMsg = (r.status && r.status !== 'ok' && r.status !== 'unrated' && r.status !== 'stale') ? (r.error || 'LLM review failed') : (a.error || null);
  const warnMsg = !errorMsg ? ((!r.status || r.status === 'unrated') ? 'Not yet reviewed' : (r.overall_score == null && r.work_priority_score == null && r.overall_priority_score == null) ? 'LLM returned no scores — review may be incomplete' : null) : null;
  const briefUrl = `/analytics/capabilities/ui?endpoint=%2Fanalytics%2Fcapabilities%2Fcustomer-brief&booking_id=${encodeURIComponent(row.booking_id)}&days=30&llm=true&display_mode=evidence`;
  const rmsUrl = `https://www.rentmystay.com/RBack/invoice_details/${encodeURIComponent(row.booking_id)}`;
  return `<tr data-booking-id="${esc(row.booking_id)}">
    <td class="nowrap"><b>${esc(row.booking_id)}</b><div class="small muted">${esc(customerValue)}</div></td>
    <td>${esc(statusValue)}</td>
    <td>${esc(stateValue)}</td>
    <td>${esc(propertyValue)}<div class="small muted">${esc(buildingValue)}</div></td>
    <td class="nowrap">${esc(day(stayFromValue))}<br><span class="muted">to</span> ${esc(day(stayToValue))}</td>
    <td class="teams small">${teamSummary(row)}</td>
    <td class="num"><b>${esc(r.overall_score ?? '-')}</b></td>
    <td class="num"><b>${esc(opsSubteamScore(row, 'desk'))}</b></td>
    <td class="num">${priorityDisplay(opsSubteamPriority(row, 'desk'))}</td>
    <td class="num"><b>${esc(opsSubteamScore(row, 'field'))}</b></td>
    <td class="num">${priorityDisplay(opsSubteamPriority(row, 'field'))}</td>
    <td class="num"><b>${esc(opsSubteamScore(row, 'asset'))}</b></td>
    <td class="num">${priorityDisplay(opsSubteamPriority(row, 'asset'))}</td>
    <td class="num">${esc(c.ops_open_tickets ?? 0)}</td>
    <td class="action">${actionLabel(a.priority_score, a.action, a.owner_team, a.evidence, errorMsg, warnMsg, [])}</td>
    <td class="small nowrap">${esc(shortDate(r.updated_at))}</td>
    <td class="nowrap">
        <a href="${esc(row.detail_url || '#')}" target="_blank" style="display:block;margin-bottom:4px;">Review</a>
        <a href="${esc(briefUrl)}" target="_blank" style="display:block;">Brief</a>
        <a href="${esc(rmsUrl)}" target="_blank" style="display:block;">RMS</a>
    </td>
    <td class="nowrap">
        <button onclick="rereview(${esc(row.booking_id)},this)" style="width:auto;padding:4px 10px;font-size:12px;font-weight:500;background:#fff;color:#172033;border:1px solid var(--border);border-radius:7px;cursor:pointer;" title="Force-refresh the LLM review for this booking">Refresh</button>
    </td>
  </tr>`;
}
async function rereview(bookingId, btn) {
  if (btn.dataset.busy === '1') return;
  btn.dataset.busy = '1';
  const orig = btn.textContent;
  btn.textContent = 'Reviewing…';
  btn.style.opacity = '0.6';
  btn.style.cursor = 'not-allowed';
  try {
    const url = `llm-rating?booking_id=${encodeURIComponent(bookingId)}&force_refresh=true&run_llm=true&customer_days=30&max_llm_messages=12&max_llm_text_chars=220&max_llm_seconds=180`;
    const res = await fetch(url);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data?.detail || data?.error || `HTTP ${res.status}`);
    const rowRes = await fetch(`dashboard?booking_id=${encodeURIComponent(bookingId)}`);
    const rowData = await rowRes.json().catch(() => ({}));
    const updatedRow = (rowData.rows || []).find(r => String(r.booking_id) === String(bookingId));
    if (updatedRow) {
      const tr = document.querySelector(`tr[data-booking-id="${bookingId}"]`);
      if (tr) {
        const tmp = document.createElement('tbody');
        tmp.innerHTML = renderRow(updatedRow);
        const newTr = tmp.firstElementChild;
        tr.replaceWith(newTr);
        const newBtn = newTr.querySelector('button');
        if (newBtn) {
          newBtn.textContent = '✓ Done';
          newBtn.style.color = 'var(--ok)';
          newBtn.dataset.busy = '1';
          setTimeout(() => { newBtn.textContent = orig; newBtn.style.color = ''; newBtn.dataset.busy = ''; }, 1500);
        }
        return;
      }
    }
    btn.textContent = '✓ Done';
    btn.style.color = 'var(--ok)';
    btn.style.opacity = '1';
    setTimeout(() => { btn.textContent = orig; btn.style.color = ''; btn.dataset.busy = ''; btn.style.cursor = ''; }, 1500);
  } catch (err) {
    btn.textContent = '✗ Error';
    btn.style.color = 'var(--bad)';
    btn.style.opacity = '1';
    btn.title = String(err);
    setTimeout(() => { btn.textContent = orig; btn.style.color = ''; btn.title = ''; btn.dataset.busy = ''; btn.style.cursor = ''; }, 4000);
  }
}
async function loadRows() {
  const tbody = document.getElementById('rows'); const status = document.getElementById('status');
  tbody.innerHTML = '<tr><td colspan="18" class="muted">Loading…</td></tr>';
  try {
    const res = await fetch('dashboard?' + paramsFromForm().toString());
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));
    const rows = data.rows || [];
    status.textContent = `${rows.length} shown of ${data.total || 0} bookings`;
    if (!rows.length) { tbody.innerHTML = '<tr><td colspan="18" class="muted">No matching bookings.</td></tr>'; return; }
    tbody.innerHTML = rows.map(renderRow).join('');
  } catch (err) {
    status.innerHTML = '<span class="error">Failed to load dashboard</span>';
    tbody.innerHTML = `<tr><td colspan="18" class="error">${esc(err.message || err)}</td></tr>`;
  }
}
document.getElementById('filters').addEventListener('submit', event => { event.preventDefault(); loadRows(); });
loadRows();
</script>
</body>
</html>
"""


@router.get("/bookings/review/dashboard-page", response_class=HTMLResponse)
def booking_dashboard_page() -> HTMLResponse:
    return HTMLResponse(BOOKING_DASHBOARD_HTML)

@router.get("/bookings/review/ops-desk-page", response_class=HTMLResponse)
def ops_desk_dashboard_page() -> HTMLResponse:
    return HTMLResponse(OPS_DESK_DASHBOARD_HTML)


@router.get("/customer-brief")
def customer_brief_get(
    booking_id: int = Query(..., description="Booking ID. This is the only supported customer brief seed."),
    days: int = Query(default=30, ge=1, le=365),
    output_format: str = Query(default="llm"),
    display_mode: Optional[str] = Query(default=None),
    llm: bool = Query(default=True),
    print_limit: int = Query(default=200, ge=0, le=5000),
    max_text: int = Query(default=220, ge=40, le=2000),
    max_llm_messages: int = Query(default=12, ge=1, le=50),
    max_llm_text_chars: int = Query(default=220, ge=40, le=2000),
    verbose: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> Any:
    payload = CustomerBriefRequest(
        booking_id=booking_id,
        days=days,
        output_format=output_format,
        display_mode=display_mode,
        llm=llm,
        print_limit=print_limit,
        max_text=max_text,
        max_llm_messages=max_llm_messages,
        max_llm_text_chars=max_llm_text_chars,
        verbose=verbose,
        schema=DEFAULT_SCHEMA,
    )
    return customer_brief(payload, db)


@router.post("/customer-brief")
def customer_brief(payload: CustomerBriefRequest, db: Session = Depends(get_db)) -> Any:
    try:
        service = CustomerBriefService(db=db, schema=payload.schema_name)
        display_mode = payload.display_mode or ("unrestricted" if payload.output_format == "unrestricted" else None)
        unrestricted = display_mode == "unrestricted"
        full_payload = service.build(
            booking_id=int(payload.booking_id),
            conversation_days=payload.days,
            unrestricted=unrestricted,
        )
        llm_context = (
            full_payload
            if unrestricted
            else service.compact_for_llm(
                full_payload,
                max_messages=payload.max_llm_messages,
                max_text_chars=payload.max_llm_text_chars,
            )
        )
        llm_prompt = (
            _build_customer_brief_analysis_prompt(
                title=f"booking-scoped customer brief for booking_id={payload.booking_id}",
                payload=llm_context,
            )
            if (payload.llm or display_mode == "llm")
            else None
        )

        # Backward compatibility: older callers used only output_format.
        if display_mode is None:
            if payload.output_format == "llm":
                result = {
                    "context_version": "customer_brief_booking_llm_with_prompt:v5",
                    "booking_id": int(payload.booking_id),
                    "llm_context": llm_context,
                    "llm_prompt": llm_prompt
                    or _build_customer_brief_analysis_prompt(
                        title=f"booking-scoped customer brief for booking_id={payload.booking_id}",
                        payload=llm_context,
                    ),
                }
                return _encode(_with_copy_blocks(result))

            if payload.output_format == "both":
                full_payload["llm_context"] = llm_context
                full_payload["llm_prompt"] = llm_prompt or _build_customer_brief_analysis_prompt(
                    title=f"booking-scoped customer brief for booking_id={payload.booking_id}",
                    payload=llm_context,
                )

            if payload.verbose:
                full_payload.setdefault("debug", {})["builder"] = "CustomerBriefService"
                full_payload["debug"]["conversation_days"] = payload.days
                full_payload["debug"]["booking_id"] = int(payload.booking_id)
                full_payload["debug"]["scope"] = "booking_id_only"

            return _encode(_with_copy_blocks(full_payload) if full_payload.get("llm_prompt") else full_payload)

        if payload.verbose:
            full_payload.setdefault("debug", {})["builder"] = "CustomerBriefService"
            full_payload["debug"]["conversation_days"] = payload.days
            full_payload["debug"]["booking_id"] = int(payload.booking_id)
            full_payload["debug"]["scope"] = "booking_id_only"

        if display_mode == "unrestricted":
            result = {
                "view": "unrestricted",
                "booking_id": int(payload.booking_id),
                "source_scope": "booking_id_only",
                "payload": full_payload,
            }
            if payload.llm:
                result["llm_context"] = llm_context
                result["llm_prompt"] = llm_prompt
            return _encode(_with_copy_blocks(result) if result.get("llm_prompt") else result)

        if display_mode == "raw":
            result = {
                "view": "raw",
                "booking_id": int(payload.booking_id),
                "source_scope": "booking_id_only",
                "payload": full_payload,
            }
            if payload.llm:
                result["llm_context"] = llm_context
                result["llm_prompt"] = llm_prompt
            return _encode(_with_copy_blocks(result) if result.get("llm_prompt") else result)

        if display_mode == "llm":
            result = {
                "view": "llm",
                "booking_id": int(payload.booking_id),
                "source_scope": "booking_id_only",
                "llm_context": llm_context,
                "llm_prompt": llm_prompt
                or _build_customer_brief_analysis_prompt(
                    title=f"booking-scoped customer brief for booking_id={payload.booking_id}",
                    payload=llm_context,
                ),
            }
            return _encode(_with_copy_blocks(result))

        result = _build_customer_brief_evidence(
            booking_id=int(payload.booking_id),
            full_payload=full_payload,
            llm_context=llm_context,
            llm_prompt=llm_prompt,
            print_limit=payload.print_limit,
            max_text=payload.max_text,
        )
        if llm_prompt:
            _with_copy_blocks(result)
        return _encode(result)
    except Exception as exc:
        raise _api_error(exc) from exc