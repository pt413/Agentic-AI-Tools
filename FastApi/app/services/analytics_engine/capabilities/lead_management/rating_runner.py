from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from .cache import LEAD_LLM_REVIEW_CACHE_TABLE, ensure_lead_review_cache_table
from .cleaning import clean_counts_for_mode, clean_events_for_mode
from .common import (
    DEFAULT_SCHEMA,
    LEAD_REVIEW_CONTEXT_VERSION,
    StaffRoleResolver,
    compact_dict,
    fmt_dt,
    q1,
    resolve_window,
    schema_ident,
    table_columns,
)
from .evidence import (
    _append_unique_id,
    enrich_lead_contacts,
    fetch_booking_confirm_rows,
    fetch_call_rows,
    fetch_email_rows,
    fetch_lead_row,
    fetch_site_visit_rows,
    fetch_travel_cart_rows,
    fetch_whatsapp_rows,
    normalized_id_list,
)
from .llm_client import _json_param, _stable_json_hash, run_openai_prompt
from .parsing import (
    _fallback_priority_score,
    _max_priority_score,
    extract_customer_followup,
    parse_actor_scores,
    parse_markdown_action_table,
    parse_overall_rating,
    parse_stakeholder_scores,
)
from .prompt import _lead_payload_for_llm, build_lead_handling_prompt, clean_lead_for_mode
from .summary import build_lead_effectiveness_summary, event_counts


class LeadCommunicationReviewRunner:
    """Runner used by FastAPI /communication/review-lead/llm-rating.

    The rest of this module is intentionally function-based for CLI and evidence
    rendering. This adapter keeps the FastAPI route stable without duplicating
    route logic in communication_routes.py.
    """

    def __init__(self, db: Session, schema: str = DEFAULT_SCHEMA):
        self.db = db
        self.schema = schema

    def build_prompt_for_lead(
        self,
        *,
        lead_id: int,
        days: int = 90,
        limit: int = 10000,
        print_limit: int = 200,
        max_text: int = 220,
        hide_automation: bool = False,
    ) -> Dict[str, Any]:
        start_dt, end_dt, window_label = resolve_window(days)
        lead_row = fetch_lead_row(self.db, self.schema, int(lead_id))
        contacts = enrich_lead_contacts(self.db, self.schema, int(lead_id), lead_row)
        role_resolver = StaffRoleResolver(self.db, self.schema)

        events: List[Dict[str, Any]] = []
        events.extend(fetch_whatsapp_rows(self.db, self.schema, int(lead_id), contacts, start_dt, end_dt, limit, max_text, role_resolver))
        events.extend(fetch_call_rows(self.db, self.schema, int(lead_id), start_dt, end_dt, limit, max_text, role_resolver))
        events.extend(fetch_email_rows(self.db, self.schema, contacts.get("emails"), start_dt, end_dt, limit, max_text, role_resolver))
        events.extend(fetch_site_visit_rows(self.db, self.schema, int(lead_id), start_dt, end_dt, limit, role_resolver))

        booking_events = fetch_booking_confirm_rows(self.db, self.schema, int(lead_id), contacts, start_dt, end_dt, limit, max_text)
        events.extend(booking_events)
        if booking_events:
            booking_ids = normalized_id_list(contacts.get("booking_ids") or contacts.get("booking_id"))
            for booking_event in booking_events:
                _append_unique_id(booking_ids, booking_event.get("booking_id"))
            if booking_ids:
                contacts["booking_ids"] = booking_ids
                contacts["booking_id"] = booking_ids[0]

        events.extend(fetch_travel_cart_rows(self.db, self.schema, contacts.get("user_ids") or contacts.get("user_id"), start_dt, end_dt, limit, max_text))

        if hide_automation:
            events = [row for row in events if not str(row.get("category") or "").startswith("auto_")]
        events.sort(key=lambda row: (fmt_dt(row.get("time")), str(row.get("channel")), str(row.get("source_id"))))

        counts = event_counts(events)
        lead_payload = _lead_payload_for_llm(contacts, lead_row)
        summary = build_lead_effectiveness_summary(lead_payload, events, counts)
        raw_payload = {
            "context_version": LEAD_REVIEW_CONTEXT_VERSION,
            "input": {
                "lead_id": int(lead_id),
                "window": window_label,
                "from": start_dt.isoformat(sep=" "),
                "to": end_dt.isoformat(sep=" "),
                "schema": self.schema,
            },
            "lead": lead_payload,
            "summary": summary,
            "counts": counts,
            "events": clean_events_for_mode(events, mode="raw"),
        }
        llm_context = {
            "context_version": LEAD_REVIEW_CONTEXT_VERSION,
            "context": compact_dict(raw_payload["input"]),
            "lead": clean_lead_for_mode(lead_payload, mode="llm"),
            "summary": summary,
            "counts": clean_counts_for_mode(counts, mode="llm"),
            "events": clean_events_for_mode(events, mode="llm", limit=print_limit or 200),
        }
        prompt = build_lead_handling_prompt(llm_context)
        context_hash = _stable_json_hash(llm_context)
        return {
            "lead_id": int(lead_id),
            "raw_payload": raw_payload,
            "llm_context": llm_context,
            "llm_prompt": prompt,
            "context_hash": context_hash,
            "lead_summary": compact_dict({
                "status": lead_payload.get("status"),
                "source": lead_payload.get("source"),
                "owner": lead_payload.get("owner"),
                "booking_ids": lead_payload.get("booking_ids"),
                "event_count": len(events),
                "counts": clean_counts_for_mode(counts, mode="evidence"),
            }),
        }

    @staticmethod
    def _cache_json(value: Any, fallback: Any) -> Any:
        if value in (None, ""):
            return fallback
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return fallback
        return fallback

    def _payload_from_cache_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        payload = compact_dict({
            "lead_id": row.get("lead_id"),
            "status": row.get("status") or "ok",
            "error": row.get("error"),
            "model": row.get("model"),
            "context_version": row.get("context_version"),
            "context_hash": row.get("context_hash"),
            "overall_score": row.get("overall_score"),
            "overall_priority_score": row.get("overall_priority_score"),
            "score": row.get("overall_score"),  # backward-compatible alias
            "lead_handling_score": row.get("lead_handling_score"),
            "customer_perspective_score": row.get("customer_perspective_score"),
            "overall_risk": row.get("overall_risk"),
            "risk": row.get("overall_risk"),  # backward-compatible alias
            "post_booking_risk": row.get("post_booking_risk"),
            "main_reason": row.get("main_reason"),
            "lead_summary": self._cache_json(row.get("summary"), {}) or {},
            "action_rows": self._cache_json(row.get("action_rows"), []) or [],
            "stakeholder_scores": self._cache_json(row.get("stakeholder_scores"), []) or [],
            "actor_scores": self._cache_json(row.get("actor_scores"), []) or [],
            "review_text": row.get("review_text"),
            "updated_at": row.get("updated_at"),
            "stale_at": row.get("stale_at"),
        })
        if payload.get("score") is None and payload.get("overall_score") is not None:
            payload["score"] = payload.get("overall_score")
        if payload.get("risk") is None and payload.get("overall_risk"):
            payload["risk"] = payload.get("overall_risk")
        return payload

    def _cache_lookup_by_lead(self, *, lead_id: int) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """Lookup by lead_id only.

        Normal user fetches should not build the prompt/context just to prove the
        hash matches. A row with status='ok' and no stale_at is authoritative.
        Any other status (for example 'stale'/'error') forces recompute when
        run_llm=True.
        """
        ensure_lead_review_cache_table(self.db, self.schema)
        table = f"{schema_ident(self.schema)}.{LEAD_LLM_REVIEW_CACHE_TABLE}"
        columns = table_columns(self.db, self.schema, LEAD_LLM_REVIEW_CACHE_TABLE)
        wanted = [
            "lead_id", "status", "error", "model", "context_version", "context_hash",
            "overall_score", "overall_priority_score", "lead_handling_score",
            "customer_perspective_score", "overall_risk", "post_booking_risk",
            "main_reason", "action_rows", "stakeholder_scores", "actor_scores",
            "summary", "review_text", "updated_at", "created_at", "stale_at",
        ]
        select_columns = [column for column in wanted if column in columns]
        if not select_columns:
            return None, {"cache_status": "unavailable", "row_status": None}

        order_by = []
        if "updated_at" in columns:
            order_by.append("updated_at DESC NULLS LAST")
        if "created_at" in columns:
            order_by.append("created_at DESC NULLS LAST")
        order_by.append("lead_id DESC")
        row = q1(
            self.db,
            f"""
            SELECT {', '.join(select_columns)}
            FROM {table}
            WHERE lead_id = :lead_id
            ORDER BY {', '.join(order_by)}
            LIMIT 1
            """,
            {"lead_id": int(lead_id)},
        )
        if not row:
            return None, {"cache_status": "miss", "row_status": None}

        row_status = str(row.get("status") or "ok").strip().lower() or "ok"
        stale_at = row.get("stale_at")
        meta = compact_dict({
            "cache_status": "hit" if row_status == "ok" and not stale_at else "stale" if stale_at else row_status,
            "row_status": row_status,
            "row_stale_at": stale_at,
            "row_context_hash": row.get("context_hash"),
            "row_updated_at": row.get("updated_at"),
            "cache_source": LEAD_LLM_REVIEW_CACHE_TABLE,
        })
        if row_status == "ok" and not stale_at:
            return self._payload_from_cache_row(row), meta
        return None, meta

    def _cache_lookup(self, *, lead_id: int, model: str, context_hash: str) -> Optional[Dict[str, Any]]:
        # Backward-compatible method name. The new policy intentionally ignores
        # model/context_hash for user fetches and trusts one status='ok' row per lead.
        cached, _meta = self._cache_lookup_by_lead(lead_id=int(lead_id))
        return cached

    def _cache_store(self, *, lead_id: int, model: str, context_hash: str, payload: Dict[str, Any]) -> None:
        # Do not overwrite a good lead row with a transient LLM/proxy failure.
        if str(payload.get("status") or "ok").lower() != "ok":
            return

        ensure_lead_review_cache_table(self.db, self.schema)
        table = f"{schema_ident(self.schema)}.{LEAD_LLM_REVIEW_CACHE_TABLE}"
        self.db.execute(
            text(f"""
            INSERT INTO {table} (
                lead_id, status, error, model, context_version, context_hash,
                overall_score, overall_priority_score, lead_handling_score,
                customer_perspective_score, overall_risk, post_booking_risk,
                main_reason, action_rows, stakeholder_scores, actor_scores,
                summary, review_text, stale_at, updated_at
            ) VALUES (
                :lead_id, :status, :error, :model, :context_version, :context_hash,
                :overall_score, :overall_priority_score, :lead_handling_score,
                :customer_perspective_score, :overall_risk, :post_booking_risk,
                :main_reason, CAST(:action_rows AS jsonb), CAST(:stakeholder_scores AS jsonb),
                CAST(:actor_scores AS jsonb), CAST(:summary AS jsonb), :review_text, NULL, NOW()
            )
            ON CONFLICT (lead_id) DO UPDATE SET
                status = EXCLUDED.status,
                error = EXCLUDED.error,
                model = EXCLUDED.model,
                context_version = EXCLUDED.context_version,
                context_hash = EXCLUDED.context_hash,
                overall_score = EXCLUDED.overall_score,
                overall_priority_score = EXCLUDED.overall_priority_score,
                lead_handling_score = EXCLUDED.lead_handling_score,
                customer_perspective_score = EXCLUDED.customer_perspective_score,
                overall_risk = EXCLUDED.overall_risk,
                post_booking_risk = EXCLUDED.post_booking_risk,
                main_reason = EXCLUDED.main_reason,
                action_rows = EXCLUDED.action_rows,
                stakeholder_scores = EXCLUDED.stakeholder_scores,
                actor_scores = EXCLUDED.actor_scores,
                summary = EXCLUDED.summary,
                review_text = EXCLUDED.review_text,
                stale_at = NULL,
                updated_at = NOW()
            """),
            {
                "lead_id": int(lead_id),
                "status": payload.get("status") or "ok",
                "error": payload.get("error"),
                "model": model,
                "context_version": LEAD_REVIEW_CONTEXT_VERSION,
                "context_hash": context_hash,
                "overall_score": payload.get("overall_score") if payload.get("overall_score") is not None else payload.get("score"),
                "overall_priority_score": payload.get("overall_priority_score"),
                "lead_handling_score": payload.get("lead_handling_score"),
                "customer_perspective_score": payload.get("customer_perspective_score"),
                "overall_risk": payload.get("overall_risk") or payload.get("risk"),
                "post_booking_risk": payload.get("post_booking_risk"),
                "main_reason": payload.get("main_reason"),
                "action_rows": _json_param(payload.get("action_rows") or []),
                "stakeholder_scores": _json_param(payload.get("stakeholder_scores") or []),
                "actor_scores": _json_param(payload.get("actor_scores") or []),
                "summary": _json_param(payload.get("lead_summary") or {}),
                "review_text": payload.get("review_text"),
            },
        )
        self.db.commit()

    def review_one(
        self,
        *,
        lead_id: int,
        days: int = 90,
        run_llm: bool = True,
        model: str = "builtin",
        limit: int = 10000,
        print_limit: int = 200,
        max_text: int = 220,
        timeout_seconds: int = 120,
        use_cache: bool = True,
        force_refresh: bool = False,
        hide_automation: bool = False,
        include_context: bool = False,
        include_prompt: bool = False,
    ) -> Dict[str, Any]:
        import time

        started = time.perf_counter()
        timings: List[Dict[str, Any]] = []

        def _record_timing(stage: str, stage_started: float, **details: Any) -> None:
            record = compact_dict({
                "stage": stage,
                "elapsed_ms": round((time.perf_counter() - stage_started) * 1000, 2),
                "total_ms": round((time.perf_counter() - started) * 1000, 2),
                **details,
            })
            timings.append(record)
            parts = [
                f"lead_id={int(lead_id)}",
                f"stage={stage}",
                f"elapsed_ms={record.get('elapsed_ms')}",
                f"total_ms={record.get('total_ms')}",
            ]
            for key in ("cache_status", "row_status", "reason", "row_context_hash", "row_stale_at", "status"):
                if record.get(key) not in (None, "", [], {}):
                    parts.append(f"{key}={record.get(key)}")
            print("[lead-review-timing] " + " ".join(parts), file=sys.stderr, flush=True)

        # Default behavior: return status='ok' cache by lead_id immediately.
        # Do not build evidence/context/hash unless cache is stale/missing or an
        # explicit refresh/debug flag is used.
        cache_meta: Dict[str, Any] = {}
        if use_cache and not force_refresh:
            cache_started = time.perf_counter()
            cached, cache_meta = self._cache_lookup_by_lead(lead_id=int(lead_id))
            _record_timing("cache_lookup", cache_started, **cache_meta)
            if cached:
                out = dict(cached)
                out.update({
                    "lead_id": int(lead_id),
                    "cached": True,
                    "cache_status": "hit",
                    "cache_strategy": "lead_id_status_cache",
                    "timings": timings,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                })
                if include_context or include_prompt:
                    build_started = time.perf_counter()
                    built_for_debug = self.build_prompt_for_lead(
                        lead_id=int(lead_id),
                        days=int(days),
                        limit=int(limit),
                        print_limit=int(print_limit),
                        max_text=int(max_text),
                        hide_automation=bool(hide_automation),
                    )
                    _record_timing("build_context_for_debug", build_started, context_hash=built_for_debug.get("context_hash"))
                    if include_context:
                        out["llm_context"] = built_for_debug.get("llm_context")
                    if include_prompt:
                        out["llm_prompt"] = built_for_debug.get("llm_prompt")
                else:
                    out.pop("llm_context", None)
                    out.pop("llm_prompt", None)
                return compact_dict(out)
        else:
            _record_timing(
                "cache_lookup_skipped",
                time.perf_counter(),
                cache_status="skipped",
                reason="force_refresh" if force_refresh else "use_cache_false",
            )

        # Missing or stale cache. If caller only wants cache, return quickly.
        if not run_llm:
            return compact_dict({
                "lead_id": int(lead_id),
                "status": "not_rated" if (cache_meta.get("cache_status") in {"miss", None}) else str(cache_meta.get("cache_status") or "not_rated"),
                "cached": False,
                "cache_status": cache_meta.get("cache_status") or ("disabled" if not use_cache else "miss"),
                "cache_strategy": "lead_id_status_cache",
                "model": model,
                "context_version": LEAD_REVIEW_CONTEXT_VERSION,
                "timings": timings,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            })

        build_started = time.perf_counter()
        built = self.build_prompt_for_lead(
            lead_id=int(lead_id),
            days=int(days),
            limit=int(limit),
            print_limit=int(print_limit),
            max_text=int(max_text),
            hide_automation=bool(hide_automation),
        )
        context_hash = built["context_hash"]
        prompt = built["llm_prompt"]
        _record_timing(
            "build_context",
            build_started,
            cache_status=cache_meta.get("cache_status"),
            context_hash=context_hash,
            event_count=(built.get("lead_summary") or {}).get("event_count"),
        )
# -------------------------The Zero Events Issue solve------------------------------------
        # No-evidence guard:
        # If no communication/timeline events are visible, do not call LLM.
        # Store a valid cache row with no quality score so dashboard stays stable.
        # ------------------------------------------------------------------
        llm_context = built.get("llm_context") or {}
        counts = llm_context.get("counts") or {}

        try:
            event_count = int(
                counts.get("events")
                or (built.get("lead_summary") or {}).get("event_count")
                or 0
            )
        except Exception:
            event_count = 0

        if event_count == 0:
            action_rows_with_lead = [
                {
                    "lead_id": int(lead_id),
                    "priority_score": 2,
                    "owner_team": "Sales Team",
                    "action": "Review lead because no communication events are visible.",
                    "evidence": "No events recorded in selected window.",
                    "overall_risk": "Not visible",
                    "risk": "Not visible",
                }
            ]

            payload = compact_dict({
                "lead_id": int(lead_id),

                # Keep status='ok' because this is a valid computed cache row.
                # If status='insufficient_data', your current _cache_store()
                # will skip storing it and the job may keep selecting it again.
                "status": "ok",

                "error": None,
                "model": model,
                "context_version": LEAD_REVIEW_CONTEXT_VERSION,
                "context_hash": context_hash,
                "cached": False,
                "cache_status": "insufficient_data_stored" if use_cache else "insufficient_data_not_stored",
                "cache_strategy": "lead_id_status_cache",
                "llm_called": False,

                # No quality score when there is no evidence.
                "overall_score": None,
                "score": None,
                "lead_handling_score": None,
                "customer_perspective_score": None,

                # Priority can still exist as work priority.
                "overall_priority_score": 2,
                "overall_risk": "Not visible",
                "risk": "Not visible",
                "post_booking_risk": "not visible",

                "main_reason": "No communication events visible for this lead in the selected window.",
                "lead_summary": built.get("lead_summary"),
                "action_rows": action_rows_with_lead,
                "stakeholder_scores": [],
                "actor_scores": [],
                "customer_followup": None,
                "review_text": "Insufficient data: no communication events visible.",
                "llm_context": built.get("llm_context"),
                "llm_prompt": prompt,
                "timings": timings,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            })

            if use_cache:
                cache_store_started = time.perf_counter()
                try:
                    self._cache_store(
                        lead_id=int(lead_id),
                        model=model,
                        context_hash=context_hash,
                        payload=payload,
                    )
                    _record_timing(
                        "cache_store",
                        cache_store_started,
                        cache_status="insufficient_data_stored",
                    )
                except Exception as exc:
                    try:
                        self.db.rollback()
                    except Exception:
                        pass
                    payload["cache_status"] = "cache_store_error"
                    payload["cache_error"] = f"{exc.__class__.__name__}: {exc}"
                    _record_timing(
                        "cache_store",
                        cache_store_started,
                        cache_status="cache_store_error",
                        error=payload["cache_error"][:300],
                    )

            if not include_context:
                payload.pop("llm_context", None)
            if not include_prompt:
                payload.pop("llm_prompt", None)

            payload["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            return compact_dict(payload)
        
        review_text = None
        error = None
        llm_started = time.perf_counter()
        try:
            review_text = run_openai_prompt(prompt, model=model, timeout_seconds=timeout_seconds)
            _record_timing("llm_call", llm_started, status="ok")
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
            _record_timing("llm_call", llm_started, status="error", error=error[:300])

        overall = parse_overall_rating(review_text or "") if review_text else {}
        action_rows = parse_markdown_action_table(review_text or "") if review_text else []
        stakeholder_scores = parse_stakeholder_scores(review_text or "") if review_text else []
        actor_scores = parse_actor_scores(review_text or "") if review_text else []
        customer_followup = extract_customer_followup(review_text or "") if review_text else None

        if review_text and not action_rows:
            action_rows = [{
                "priority_score": None,
                "owner_team": "Monitoring team",
                "action": "Review LLM output manually; action table could not be parsed.",
                "evidence": "LLM returned text but not the expected markdown action table.",
            }]

        action_rows_with_lead = []
        for row in action_rows:
            action_rows_with_lead.append(compact_dict({
                "lead_id": int(lead_id),
                "priority_score": row.get("priority_score"),
                "owner_team": row.get("owner_team"),
                "action": row.get("action"),
                "evidence": row.get("evidence"),
                "overall_risk": overall.get("overall_risk") or overall.get("risk"),
                "risk": overall.get("overall_risk") or overall.get("risk"),
                "overall_score": overall.get("overall_score") if overall.get("overall_score") is not None else overall.get("score"),
                "score": overall.get("overall_score") if overall.get("overall_score") is not None else overall.get("score"),
            }))

        overall_score = overall.get("overall_score") if overall.get("overall_score") is not None else overall.get("score")
        overall_risk = overall.get("overall_risk") or overall.get("risk")
        overall_priority_score = (
            overall.get("overall_priority_score")
            or _max_priority_score(action_rows_with_lead, stakeholder_scores, actor_scores)
            or _fallback_priority_score(overall_risk=overall_risk, overall_score=overall_score)
        )

        payload = compact_dict({
            "lead_id": int(lead_id),
            "status": "error" if error else "ok",
            "error": error,
            "model": model,
            "context_version": LEAD_REVIEW_CONTEXT_VERSION,
            "context_hash": context_hash,
            "cached": False,
            "cache_status": "stored" if use_cache and not error else "disabled" if not use_cache else "error_not_stored",
            "cache_strategy": "lead_id_status_cache",
            "overall_score": overall_score,
            "overall_priority_score": overall_priority_score,
            "score": overall_score,  # backward-compatible alias for existing UI
            "customer_perspective_score": overall.get("customer_perspective_score"),
            "lead_handling_score": overall.get("lead_handling_score"),
            "overall_risk": overall_risk,
            "risk": overall_risk,  # backward-compatible alias for existing UI
            "post_booking_risk": overall.get("post_booking_risk"),
            "main_reason": overall.get("main_reason"),
            "lead_summary": built.get("lead_summary"),
            "action_rows": action_rows_with_lead,
            "stakeholder_scores": stakeholder_scores,
            "actor_scores": actor_scores,
            "customer_followup": customer_followup,
            "review_text": review_text,
            "llm_context": built.get("llm_context"),
            "llm_prompt": prompt,
            "timings": timings,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        })

        if use_cache:
            cache_store_started = time.perf_counter()
            try:
                self._cache_store(lead_id=int(lead_id), model=model, context_hash=context_hash, payload=payload)
                _record_timing("cache_store", cache_store_started, cache_status="stored" if not error else "error_not_stored")
            except Exception as exc:
                try:
                    self.db.rollback()
                except Exception:
                    pass
                payload["cache_status"] = "cache_store_error"
                payload["cache_error"] = f"{exc.__class__.__name__}: {exc}"
                _record_timing("cache_store", cache_store_started, cache_status="cache_store_error", error=payload["cache_error"][:300])

        if not include_context:
            payload.pop("llm_context", None)
        if not include_prompt:
            payload.pop("llm_prompt", None)
        payload["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return compact_dict(payload)
