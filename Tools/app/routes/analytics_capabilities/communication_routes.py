from __future__ import annotations

from datetime import date
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.routes.analytics_capabilities.common import (
    DEFAULT_SCHEMA,
    LeadCommunicationReviewRequest,
    NumberCommunicationReviewRequest,
    WhatsAppInspectRequest,
    _api_error,
    _build_communication_analysis_prompt,
    _build_evidence_payload,
    _encode,
    _namespace,
    _with_copy_blocks,
)
import app.services.analytics_engine.capabilities.review_lead_communication as lead_review
import app.services.analytics_engine.capabilities.review_number_communication as number_review
import app.services.analytics_engine.capabilities.whatsapp_conversation_inspector as wa_inspector


router = APIRouter()


@router.post("/communication/review-lead")
def review_lead_communication(payload: LeadCommunicationReviewRequest, db: Session = Depends(get_db)) -> Any:
    try:
        start_dt, end_dt, window_label = lead_review.resolve_window(payload.days)
        lead_row = lead_review.fetch_lead_row(db, payload.schema_name, payload.lead_id)
        contacts = lead_review.enrich_lead_contacts(db, payload.schema_name, payload.lead_id, lead_row)
        role_resolver = lead_review.StaffRoleResolver(db, payload.schema_name)
        unrestricted = payload.display_mode == "unrestricted"
        text_limit = 1_000_000 if unrestricted else payload.max_text

        rows: list[dict[str, Any]] = []
        rows.extend(lead_review.fetch_whatsapp_rows(db, payload.schema_name, payload.lead_id, contacts, start_dt, end_dt, payload.limit, text_limit, role_resolver))
        rows.extend(lead_review.fetch_call_rows(db, payload.schema_name, payload.lead_id, start_dt, end_dt, payload.limit, text_limit, role_resolver))
        rows.extend(lead_review.fetch_email_rows(db, payload.schema_name, contacts.get("emails"), start_dt, end_dt, payload.limit, text_limit, role_resolver))
        rows.extend(lead_review.fetch_site_visit_rows(db, payload.schema_name, payload.lead_id, start_dt, end_dt, payload.limit, role_resolver))

        booking_events = lead_review.fetch_booking_confirm_rows(db, payload.schema_name, payload.lead_id, contacts, start_dt, end_dt, payload.limit, text_limit)
        rows.extend(booking_events)
        if booking_events:
            booking_ids = lead_review.normalized_id_list(contacts.get("booking_ids") or contacts.get("booking_id"))
            for booking_event in booking_events:
                lead_review._append_unique_id(booking_ids, booking_event.get("booking_id"))
            if booking_ids:
                contacts["booking_ids"] = booking_ids
                contacts["booking_id"] = booking_ids[0]

        rows.extend(lead_review.fetch_travel_cart_rows(db, payload.schema_name, contacts.get("user_ids") or contacts.get("user_id"), start_dt, end_dt, payload.limit, text_limit))
        rows.sort(key=lambda r: (lead_review.fmt_dt(r.get("time")), str(r.get("channel") or ""), str(r.get("source_id") or "")))

        window = {"start": start_dt, "end": end_dt, "label": window_label}
        visible_rows = lead_review.clean_events_for_mode(
            rows,
            mode="raw",
            limit=None if unrestricted else (payload.print_limit if payload.print_limit else None),
        )
        evidence_rows = lead_review.clean_events_for_mode(rows, mode="evidence")
        llm_rows = lead_review.clean_events_for_mode(
            rows,
            mode="raw" if unrestricted else "llm",
            limit=None if unrestricted else (payload.print_limit or 200),
        )
        counts = lead_review.event_counts(rows)
        lead_payload = lead_review._lead_payload_for_llm(contacts, lead_row)
        summary = lead_review.build_lead_effectiveness_summary(lead_payload, rows, counts)

        base_payload = {
            "context_version": lead_review.LEAD_REVIEW_CONTEXT_VERSION,
            "input": {
                "lead_id": payload.lead_id,
                "window": window_label,
                "from": start_dt.isoformat(sep=" "),
                "to": end_dt.isoformat(sep=" "),
                "schema": payload.schema_name,
            },
            "lead": lead_payload,
            "summary": summary,
            "counts": counts,
            "events": visible_rows,
        }
        raw_result = {
            "view": "raw",
            "lead_id": payload.lead_id,
            "window": window,
            "contacts": contacts,
            "row_count": len(rows),
            "rows": visible_rows,
            "source_scope": lead_review.LEAD_REVIEW_SOURCE_SCOPE,
        }

        llm_context = None
        llm_prompt = None
        if payload.llm or payload.display_mode in {"llm", "unrestricted"}:
            if payload.llm or payload.display_mode in {"llm", "unrestricted"}:
                llm_context = lead_review.build_lead_review_v11_payload(
                    lead_id=payload.lead_id,
                    window_label=window_label,
                    start_dt=start_dt,
                    end_dt=end_dt,
                    schema_name=payload.schema_name,
                    lead_payload=lead_review.clean_lead_for_mode(base_payload["lead"], mode="llm"),
                    rows=llm_rows,
                    limit=None if unrestricted else (payload.print_limit or 200),
                )
                llm_prompt = lead_review.build_lead_handling_prompt(llm_context)

        if payload.display_mode == "raw":
            result = raw_result
        elif payload.display_mode == "unrestricted":
            # Keep the same evidence-style review shape, but do not apply print_limit
            # or normal max_text truncation. Full rows are kept separately for debug/copy.
            result = _build_evidence_payload(
                title="Lead Communication Review - unrestricted",
                identity={"lead_id": payload.lead_id},
                window=window,
                contacts=contacts,
                rows=visible_rows,
                print_limit=0,
                max_text=text_limit,
                extra={
                    "source_scope": lead_review.LEAD_REVIEW_SOURCE_SCOPE,
                    "context_version": lead_review.LEAD_REVIEW_CONTEXT_VERSION + ":unrestricted",
                    "summary": summary,
                    "counts": lead_review.clean_counts_for_mode(counts, mode="evidence"),
                    "display_mode": "unrestricted",
                },
            )
            result["view"] = "unrestricted"
            result["row_count"] = len(rows)
            result["rows"] = visible_rows
            result.setdefault("copy_blocks", {})["timeline_text"] = "\n".join(
                str(item.get("line") or "").strip()
                for item in (result.get("timeline") or [])
                if str(item.get("line") or "").strip()
            )
            if llm_context:
                result["llm_context"] = llm_context
            if llm_prompt:
                result["llm_prompt"] = llm_prompt
        elif payload.display_mode == "llm":
            result = {
                "view": "llm",
                "lead_id": payload.lead_id,
                "window": window,
                "contacts": contacts,
                "row_count": len(rows),
                "source_scope": lead_review.LEAD_REVIEW_SOURCE_SCOPE,
                "llm_context": llm_context,
                "llm_prompt": llm_prompt,
            }
        else:
            result = _build_evidence_payload(
                title="Lead Communication Review",
                identity={"lead_id": payload.lead_id},
                window=window,
                contacts=contacts,
                rows=evidence_rows,
                print_limit=payload.print_limit,
                max_text=payload.max_text,
                extra={
                    "source_scope": lead_review.LEAD_REVIEW_SOURCE_SCOPE,
                    "context_version": lead_review.LEAD_REVIEW_CONTEXT_VERSION,
                    "summary": summary,
                    "counts": lead_review.clean_counts_for_mode(counts, mode="evidence"),
                },
            )
            if llm_prompt:
                result["llm_prompt"] = llm_prompt

        if llm_prompt:
            _with_copy_blocks(result)
        return _encode(result)
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/communication/review-number")
def review_number_communication(payload: NumberCommunicationReviewRequest, db: Session = Depends(get_db)) -> Any:
    try:
        phone_p10 = number_review.phone_last10(payload.phone)
        if not phone_p10:
            raise ValueError("Provide a valid phone with 10 digits or 12 digits.")

        start_dt, end_dt, window_label = number_review.resolve_window(payload.days, payload.from_date, payload.to_date)
        rows: list[dict[str, Any]] = []
        rows.extend(number_review.fetch_calls(db, payload.schema_name, phone_p10, payload.role, start_dt, end_dt, payload.limit, payload.max_text))
        rows.extend(number_review.fetch_whatsapp(db, payload.schema_name, phone_p10, payload.role, start_dt, end_dt, payload.limit, payload.max_text))
        rows = [row for row in rows if number_review.keep_row(row, focus=payload.focus, hide_automation=payload.hide_automation)]
        rows.sort(key=lambda r: (number_review.fmt_dt(r.get("event_time")), str(r.get("channel") or ""), str(r.get("source_id") or "")))

        counts = {
            "events": len(rows),
            "calls": sum(1 for r in rows if r.get("channel") == "call"),
            "whatsapp": sum(1 for r in rows if r.get("channel") == "whatsapp"),
            "business_to_counterparty": sum(1 for r in rows if r.get("business_flow") == "business_to_counterparty"),
            "counterparty_to_business": sum(1 for r in rows if r.get("business_flow") == "counterparty_to_business"),
            "needs_review": sum(1 for r in rows if r.get("needs_review")),
        }
        window = {"start": start_dt, "end": end_dt, "label": window_label}
        visible_rows = rows[: payload.print_limit] if payload.print_limit else rows
        seed = number_review.show_phone(phone_p10)

        raw_result: dict[str, Any] = {
            "view": "raw",
            "seed": seed,
            "seed_role": payload.role,
            "window": window,
            "source_scope": "phone_only_exact_last10",
            "counts": counts,
            "row_count": len(rows),
            "rows": visible_rows,
        }

        llm_context = None
        llm_prompt = None
        if payload.llm or payload.display_mode == "llm":
            llm_context = {
                "context_version": "review_number_communication:v2_phone_only",
                "input": {
                    "phone": seed,
                    "role": payload.role,
                    "window": window_label,
                    "from": start_dt.isoformat(sep=" "),
                    "to": end_dt.isoformat(sep=" "),
                    "schema": payload.schema_name,
                },
                "source_scope": "phone_only_exact_last10",
                "counts": counts,
                "events": visible_rows,
            }
            llm_prompt = _build_communication_analysis_prompt(
                title=f"phone-only communication review for {seed}",
                payload=llm_context,
            )

        if payload.display_mode == "raw":
            result = raw_result
        elif payload.display_mode == "llm":
            result = {
                "view": "llm",
                "seed": seed,
                "seed_role": payload.role,
                "window": window,
                "source_scope": "phone_only_exact_last10",
                "counts": counts,
                "row_count": len(rows),
                "llm_context": llm_context,
                "llm_prompt": llm_prompt,
            }
        else:
            result = _build_evidence_payload(
                title="Number Communication Review",
                identity={"seed": seed, "seed_role": payload.role},
                window=window,
                resolved_universe={"phone": seed, "match": "exact_last10_only"},
                rows=rows,
                print_limit=payload.print_limit,
                max_text=payload.max_text,
                extra={"source_scope": "phone_only_exact_last10", "counts": counts},
            )
            if llm_context:
                result["llm_context"] = llm_context
            if llm_prompt:
                result["llm_prompt"] = llm_prompt

        if llm_prompt:
            _with_copy_blocks(result)
        return _encode(result)
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/whatsapp/inspect")
def inspect_whatsapp(payload: WhatsAppInspectRequest, db: Session = Depends(get_db)) -> Any:
    try:
        args = _namespace(
            phone=payload.phone,
            actor=payload.actor,
            lead_id=payload.lead_id,
            thread_key=payload.thread_key,
            remote_jid=payload.remote_jid,
            admin_number=payload.admin_number,
            customer_number=payload.customer_number,
            list_threads=payload.list_threads,
            include_overall_summary=payload.include_overall_summary,
            schema=payload.schema_name,
            limit=payload.limit,
            json=True,
        )

        anchor = wa_inspector.resolve_anchor(db, args.schema, args)
        threads = wa_inspector.fetch_threads(db, args.schema, anchor, args) if args.list_threads or wa_inspector.has_explicit_thread_selector(args) else []
        overall = (
            wa_inspector.build_customer_overall_summary(db, args.schema, anchor.get("person_ids") or [])
            if args.include_overall_summary
            else None
        )
        list_only = bool(args.list_threads and not wa_inspector.has_explicit_thread_selector(args))

        if list_only:
            messages: list[dict[str, Any]] = []
            turns: list[dict[str, Any]] = []
            summary: dict[str, Any] = {}
            latest_message = None
            participants: list[dict[str, Any]] = []
        else:
            messages = wa_inspector.fetch_messages(db, args.schema, anchor, args)
            turns = wa_inspector.build_whatsapp_turns(messages)
            summary = wa_inspector.build_whatsapp_conversation_summary(turns)
            latest_message = messages[-1] if messages else None
            participants = wa_inspector.fetch_latest_participants(
                db,
                args.schema,
                latest_event_id=latest_message.get("event_id") if latest_message else None,
                latest_row=latest_message,
            )

        latest_ontology = (latest_message or {}).get("message_ontology") or {}
        return _encode(
            {
                "search_input": payload.model_dump(by_alias=True),
                "resolved": anchor,
                "threads": threads,
                "list_only": list_only,
                "customer_overall_summary": overall,
                "latest_message": latest_message,
                "latest_message_tags": latest_ontology,
                "conversation_summary": summary,
                "turns": turns,
                "messages": messages,
                "participants": participants,
            }
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/communication/review-lead/llm-rating")
def review_lead_llm_rating_get(
    lead_id: int = Query(...),
    days: int = Query(default=90, ge=1, le=365),
    limit: int = Query(default=10000, ge=1, le=100000),
    max_text: int = Query(default=220, ge=40, le=2000),
    print_limit: int = Query(default=200, ge=1, le=5000),
    run_llm: bool = Query(default=True),
    model: str = Query(default="builtin"),
    timeout_seconds: int = Query(default=120, ge=30, le=300),
    use_cache: bool = Query(default=True, description="Reuse stored LLM rating when evidence context_hash is unchanged."),
    force_refresh: bool = Query(default=False, description="Ignore matching cache and call the LLM again."),
    hide_automation: bool = Query(default=False),
    include_context: bool = Query(default=False),
    include_prompt: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> Any:
    try:
        runner = lead_review.LeadCommunicationReviewRunner(db=db, schema=DEFAULT_SCHEMA)
        return _encode(
            runner.review_one(
                lead_id=lead_id,
                days=days,
                run_llm=run_llm,
                model=model,
                limit=limit,
                print_limit=print_limit,
                max_text=max_text,
                timeout_seconds=timeout_seconds,
                use_cache=use_cache,
                force_refresh=force_refresh,
                hide_automation=hide_automation,
                include_context=include_context,
                include_prompt=include_prompt,
            )
        )
    except Exception as exc:
        raise _api_error(exc) from exc




@router.get("/communication/review-lead/dashboard")
def review_lead_dashboard_get(
    lead_id: Optional[int] = Query(default=None),
    handled_by: Optional[str] = Query(default=None, description="Partial username/owner search."),
    status: Optional[str] = Query(default=None, description="Lead status, e.g. Waiting, Booked."),
    source: Optional[str] = Query(default=None, description="Lead source/origin, e.g. SiteVisit, SignUp."),
    risk: Optional[str] = Query(default=None, description="Overall risk: Low, Medium, High."),
    review_status: Optional[str] = Query(default=None, description="Rating cache status: ok, stale, unrated."),
    min_score: Optional[float] = Query(default=None, ge=0, le=10),
    max_score: Optional[float] = Query(default=None, ge=0, le=10),
    min_priority: Optional[float] = Query(default=None, ge=0, le=10),
    max_priority: Optional[float] = Query(default=None, ge=0, le=10),
    search: Optional[str] = Query(default=None, description="Free-text search across lead id, owner, status, source, and reason."),
    only_rated: bool = Query(default=True, description="Use true for dashboard of rated leads; false includes unrated leads."),
    include_actions: bool = Query(default=True),
    sort_by: Literal[
        "lead_id", "created_at", "closed_at", "handled_by", "status", "source",
        "score", "overall_score", "priority", "overall_priority_score", "risk", "updated_at", "last_activity_at",
    ] = Query(default="priority"),
    sort_dir: Literal["asc", "desc"] = Query(default="desc"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> Any:
    try:
        return _encode(
            lead_review.list_lead_dashboard_rows(
                db=db,
                schema=DEFAULT_SCHEMA,
                lead_id=lead_id,
                handled_by=handled_by,
                status=status,
                source=source,
                risk=risk,
                review_status=review_status,
                min_score=min_score,
                max_score=max_score,
                min_priority=min_priority,
                max_priority=max_priority,
                search=search,
                only_rated=only_rated,
                include_actions=include_actions,
                sort_by=sort_by,
                sort_dir=sort_dir,
                limit=limit,
                offset=offset,
            )
        )
    except Exception as exc:
        raise _api_error(exc) from exc


LEAD_DASHBOARD_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Lead Handling Dashboard</title>
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
    .action { max-width:320px; }
    .nowrap { white-space:nowrap; }
    .people { min-width:220px; max-width:340px; }
    .people .small { margin-bottom:4px; line-height:1.25; }
    .people-more-link {
    display:inline-block;
    border:0;
    background:transparent;
    color:#175cd3;
    font-size:12px;
    font-weight:600;
    padding:0;
    margin-top:4px;
    cursor:pointer;
    text-align:left;
    }
    .people-more-link:hover { text-decoration:underline; }
    .people-more-body {
    display:none;
    margin-top:4px;
    padding-top:4px;
    border-top:1px dashed var(--border);
    }
    .statusbar { padding:10px 14px; display:flex; justify-content:space-between; color:var(--muted); font-size:13px; border-top:1px solid var(--border); }
    .error { color:var(--bad); }
    @media (max-width: 1200px) { .filters { grid-template-columns: repeat(3, minmax(120px, 1fr)); } }
  </style>
</head>
<body>
  <header>
    <h1>Lead Handling Dashboard</h1>
    <div class="sub">Compact lead details + cached lead_communication_review score. This page does not call the LLM per row.</div>
  </header>

  <section class="panel">
    <form id="filters" class="filters">
      <div><label>Lead ID</label><input name="lead_id" placeholder="401676" /></div>
      <div><label>Handled by</label><input name="handled_by" placeholder="username" /></div>
      <div><label>Status</label><input name="status" placeholder="Waiting" /></div>
      <div><label>Source</label><input name="source" placeholder="SiteVisit" /></div>
      <div><label>Risk</label><select name="risk"><option value="">Any</option><option>High</option><option>Medium</option><option>Low</option></select></div>
      <div><label>Min score</label><input name="min_score" type="number" min="0" max="10" step="1" /></div>
      <div><label>Sort</label><select name="sort_by"><option value="priority">Priority</option><option value="score">Overall score</option><option value="lead_id">Lead ID</option><option value="handled_by">Handled by</option><option value="created_at">Created</option><option value="updated_at">Rated at</option></select></div>
      <div><label>Direction</label><select name="sort_dir"><option value="desc">Desc</option><option value="asc">Asc</option></select></div>
      <div><label>Search</label><input name="search" placeholder="reason/source/owner" /></div>
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
            <th>Lead</th><th>Status</th><th>Source</th><th>Handled by</th><th>People scores</th>
            <th class="num">Score</th><th class="num">Priority</th><th class="num">Lead</th>
            <th>Risk</th><th class="num">Events</th><th class="num">Calls</th><th class="num">WA</th><th class="num">Email</th>
            <th>Top action</th><th>Rated at</th><th></th>
          </tr>
        </thead>
        <tbody id="rows"><tr><td colspan="17" class="muted">Loading…</td></tr></tbody>
      </table>
    </div>
    <div class="statusbar"><span id="status">-</span><span class="small">Tip: sort by score ascending to find weak handling, or priority descending for urgent review.</span></div>
  </section>

<script>
function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}
function riskPill(value) {
  const risk = String(value || '').toLowerCase();
  if (!risk) return '<span class="muted">-</span>';
  return `<span class="pill risk-${esc(risk)}">${esc(value)}</span>`;
}
function shortDate(value) {
  if (!value) return '-';
  return String(value).replace('T', ' ').slice(0, 19);
}
function peopleScoresHtml(row) {
  const actors = ((row.actors || {}).scorecard || row.actor_scores || []);
  if (!Array.isArray(actors) || !actors.length) {
    return '<span class="muted">-</span>';
  }

  function actorLine(actor) {
    const name = actor.actor_entity || actor.actor || actor.name || actor.username || '-';
    const role = actor.role_team || actor.role || actor.team || '';
    const score = actor.score ?? '-';
    const priority = actor.priority_score ?? '';
    const action = actor.action || '';
    const evidence = actor.evidence || '';

    return `
      <div class="small people-score-item" title="${esc(evidence)}">
        <b>${esc(name)}</b>
        ${role ? `<span class="muted">(${esc(role)})</span>` : ''}
        <span class="pill">${esc(score)}/10</span>
        ${priority !== '' ? `<span class="muted">P:${esc(priority)}</span>` : ''}
        ${action ? `<div class="muted">${esc(action)}</div>` : ''}
      </div>
    `;
  }

  const visibleHtml = actors.slice(0, 3).map(actorLine).join('');

  if (actors.length <= 3) {
    return visibleHtml;
  }

  const hiddenHtml = actors.slice(3).map(actorLine).join('');

  return `
    ${visibleHtml}
    <button type="button" class="people-more-link" onclick="expandPeopleScores(this)">
      +${actors.length - 3} more
    </button>
    <div class="people-more-body">
      ${hiddenHtml}
    </div>
  `;
}

function expandPeopleScores(button) {
  const body = button.nextElementSibling;
  if (body) {
    body.style.display = 'block';
  }
  button.style.display = 'none';
}

function paramsFromForm() {
  const data = new FormData(document.getElementById('filters'));
  const params = new URLSearchParams();
  for (const [key, value] of data.entries()) {
    if (String(value).trim() !== '') params.set(key, String(value).trim());
  }
  return params;
}
async function loadRows() {
  const tbody = document.getElementById('rows');
  const status = document.getElementById('status');
  tbody.innerHTML = '<tr><td colspan="17" class="muted">Loading…</td></tr>';
  try {
    const res = await fetch('dashboard?' + paramsFromForm().toString());
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));
    const rows = data.rows || [];
    status.textContent = `${rows.length} shown of ${data.total || 0} leads`;
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="17" class="muted">No matching leads.</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(row => {
      const lead = row.lead || {};
      const rating = row.rating || {};
      const counts = row.counts || {};
      const action = row.top_action || {};
      return `<tr>
        <td class="nowrap"><b>${esc(row.lead_id)}</b><div class="small muted">${esc(lead.customer_name || lead.contact_phone || '')}</div></td>
        <td>${esc(lead.status || '-')}</td>
        <td>${esc(lead.source || '-')}</td>
        <td>${esc(lead.handled_by || '-')}</td>
        <td class="people">${peopleScoresHtml(row)}</td>
        <td class="num"><b>${esc(rating.overall_score ?? '-')}</b></td>
        <td class="num">${esc(rating.overall_priority_score ?? '-')}</td>
        <td class="num">${esc(rating.lead_handling_score ?? '-')}</td>
        <td>${riskPill(rating.overall_risk)}</td>
        <td class="num">${esc(counts.events ?? '-')}</td>
        <td class="num">${esc(counts.calls ?? '-')}</td>
        <td class="num">${esc(counts.whatsapp ?? '-')}</td>
        <td class="num">${esc(counts.emails ?? '-')}</td>
        <td class="action"><b>${esc(action.owner_team || '')}</b> ${esc(action.action || rating.main_reason || '-')}<div class="small muted">${esc(action.evidence || '')}</div></td>
        <td class="small nowrap">${esc(shortDate(rating.updated_at))}</td>
        <td><a href="${esc(row.detail_url || '#')}" target="_blank">Open</a></td>
      </tr>`;
    }).join('');
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


@router.get("/communication/review-lead/dashboard-page", response_class=HTMLResponse)
def review_lead_dashboard_page() -> HTMLResponse:
    return HTMLResponse(LEAD_DASHBOARD_HTML)


@router.get("/communication/review-lead/llm_rating")
def review_lead_llm_rating_get_alias(
    lead_id: int = Query(...),
    days: int = Query(default=90, ge=1, le=365),
    limit: int = Query(default=10000, ge=1, le=100000),
    max_text: int = Query(default=220, ge=40, le=2000),
    print_limit: int = Query(default=200, ge=1, le=5000),
    run_llm: bool = Query(default=True),
    model: str = Query(default="builtin"),
    timeout_seconds: int = Query(default=120, ge=30, le=300),
    use_cache: bool = Query(default=True),
    force_refresh: bool = Query(default=False),
    hide_automation: bool = Query(default=False),
    include_context: bool = Query(default=False),
    include_prompt: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> Any:
    return review_lead_llm_rating_get(
        lead_id=lead_id,
        days=days,
        limit=limit,
        max_text=max_text,
        print_limit=print_limit,
        run_llm=run_llm,
        model=model,
        timeout_seconds=timeout_seconds,
        use_cache=use_cache,
        force_refresh=force_refresh,
        hide_automation=hide_automation,
        include_context=include_context,
        include_prompt=include_prompt,
        db=db,
    )

@router.get("/communication/review-lead")
def review_lead_communication_get(
    lead_id: int = Query(...),
    days: int = Query(default=90, ge=1, le=365),
    limit: int = Query(default=10000, ge=1, le=100000),
    max_text: int = Query(default=180, ge=40, le=2000),
    print_limit: int = Query(default=200, ge=0, le=5000),
    llm: bool = Query(default=True),
    display_mode: Literal["evidence", "llm", "raw", "unrestricted"] = Query(default="evidence"),
    db: Session = Depends(get_db),
) -> Any:
    payload = LeadCommunicationReviewRequest(
        lead_id=lead_id,
        days=days,
        limit=limit,
        max_text=max_text,
        print_limit=print_limit,
        llm=llm,
        display_mode=display_mode,
        schema=DEFAULT_SCHEMA,
    )
    return review_lead_communication(payload, db)


@router.get("/communication/review-number")
def review_number_communication_get(
    phone: str = Query(..., description="Phone. Accepts 10 digits or 12 digits; matching uses last 10 digits."),
    days: int = Query(default=30, ge=1, le=365),
    from_date: Optional[date] = Query(default=None),
    to_date: Optional[date] = Query(default=None),
    limit: int = Query(default=10000, ge=1, le=100000),
    max_text: int = Query(default=220, ge=40, le=2000),
    print_limit: int = Query(default=200, ge=0, le=10000),
    llm: bool = Query(default=True),
    role: Literal["any", "admin", "counterparty"] = Query(default="any"),
    view: Literal["clean", "raw"] = Query(default="clean"),
    focus: Literal["all", "review", "manual", "automation"] = Query(default="all"),
    hide_automation: bool = Query(default=False),
    display_mode: Literal["evidence", "llm", "raw"] = Query(default="evidence"),
    db: Session = Depends(get_db),
) -> Any:
    payload = NumberCommunicationReviewRequest(
        phone=phone,
        days=days,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        max_text=max_text,
        print_limit=print_limit,
        llm=llm,
        role=role,
        view=view,
        focus=focus,
        hide_automation=hide_automation,
        display_mode=display_mode,
        schema=DEFAULT_SCHEMA,
    )
    return review_number_communication(payload, db)


@router.get("/whatsapp/inspect")
def inspect_whatsapp_get(
    phone: Optional[str] = Query(default=None),
    actor: Optional[str] = Query(default=None),
    lead_id: Optional[int] = Query(default=None),
    thread_key: Optional[str] = Query(default=None),
    remote_jid: Optional[str] = Query(default=None),
    admin_number: Optional[str] = Query(default=None),
    customer_number: Optional[str] = Query(default=None),
    list_threads: bool = Query(default=False),
    include_overall_summary: bool = Query(default=False),
    limit: int = Query(default=120, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> Any:
    payload = WhatsAppInspectRequest(
        phone=phone,
        actor=actor,
        lead_id=lead_id,
        thread_key=thread_key,
        remote_jid=remote_jid,
        admin_number=admin_number,
        customer_number=customer_number,
        list_threads=list_threads,
        include_overall_summary=include_overall_summary,
        limit=limit,
        schema=DEFAULT_SCHEMA,
    )
    return inspect_whatsapp(payload, db)
