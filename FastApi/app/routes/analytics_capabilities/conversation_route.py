from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.routes.analytics_capabilities.common import _api_error, _encode
from app.services.analytics_engine.capabilities.booking_management.common import DEFAULT_MODEL
from app.services.analytics_engine.capabilities.booking_management.communication_service import (
    build_whatsapp_thread_llm_context,
    build_whatsapp_thread_llm_prompt,
    build_whatsapp_thread_summaries,
    build_whatsapp_thread_timeline,
    get_whatsapp_conversations,
    parse_whatsapp_thread_llm_review,
)
from app.services.analytics_engine.capabilities.booking_management.llm_client import run_openai_prompt


router = APIRouter(
    prefix="/comm/analysis",
    tags=["analytics_capabilities_conversation"],
)


@router.get("/conversation")
def conversation(
    admin_number: str = Query(
        default="918553635971",
        description="Admin WhatsApp number. Defaults to the exact query number.",
    ),
    start_timestamp: datetime = Query(
        default=datetime(2026, 5, 19, 0, 0, 0),
        description="Lower bound for message timestamp. Defaults to the exact query timestamp.",
    ),
    end_timestamp: datetime | None = Query(
        default=None,
        description="Optional inclusive upper bound for message timestamp.",
    ),
    conversation_type: Literal["all", "inbound", "outbound"] = Query(
        default="all",
        description="all = no direction filter, inbound = incoming/inbound, outbound = outgoing/outbound.",
    ),
    device: str = Query(
        default="baileys",
        description="Message source device. Defaults to the exact query device.",
    ),
    cx_prefix: str = Query(
        default="91",
        description="Customer number prefix filter. Defaults to the exact query prefix.",
    ),
    cx_number: Optional[str] = Query(
        default=None,
        description="Optional exact customer number. Required for single-thread LLM review mode.",
    ),
    llm: bool = Query(
        default=False,
        description="Include LLM-ready context/prompt for one exact customer thread.",
    ),
    run_llm: bool = Query(
        default=False,
        description="Run the LLM immediately for one exact customer thread and return the parsed review.",
    ),
    model: str = Query(
        default=DEFAULT_MODEL,
        description="Model name used only when run_llm=true.",
    ),
    timeout_seconds: int = Query(
        default=120,
        ge=30,
        le=300,
        description="LLM timeout used only when run_llm=true.",
    ),
    display_mode: Literal["raw", "evidence", "llm"] = Query(
        default="raw",
        description="raw = message rows, evidence = deterministic thread summaries, llm = one-customer LLM review context/prompt.",
    ),
    print_limit: int = Query(
        default=200,
        ge=0,
        le=5000,
        description="How many timeline rows to include in evidence/llm views.",
    ),
    max_text: int = Query(
        default=220,
        ge=40,
        le=2000,
        description="Max text chars per message in evidence/llm views.",
    ),
    db: Session = Depends(get_db),
) -> Any:
    """Return raw WhatsApp rows or hybrid deterministic/LLM-ready conversation analysis."""
    try:
        rows = get_whatsapp_conversations(
            db,
            admin_number=admin_number,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            conversation_type=conversation_type,
            device=device,
            cx_prefix=cx_prefix,
            cx_number=cx_number,
        )
        query_payload = {
            "table": "public.messages",
            "device": device,
            "admin_number": admin_number,
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
            "cx_prefix": cx_prefix,
            "cx_number": cx_number,
            "conversation_type": conversation_type,
            "display_mode": display_mode,
            "run_llm": run_llm,
        }

        if display_mode == "raw":
            return _encode(
                {
                    "status": "ok",
                    "view": "raw",
                    "query": query_payload,
                    "count": len(rows),
                    "rows": rows,
                }
            )

        thread_summaries = build_whatsapp_thread_summaries(rows)
        warnings: list[str] = []
        if conversation_type != "all":
            warnings.append("analysis_is_running_on_a_direction_filtered_subset")

        if display_mode == "evidence" and not cx_number:
            eligible_thread_count = sum(
                1 for thread in thread_summaries if thread.get("score_status") == "eligible"
            )
            return _encode(
                {
                    "status": "ok",
                    "view": "evidence",
                    "title": "WhatsApp Conversation Threads",
                    "query": query_payload,
                    "message_count": len(rows),
                    "thread_count": len(thread_summaries),
                    "eligible_thread_count": eligible_thread_count,
                    "no_score_thread_count": len(thread_summaries) - eligible_thread_count,
                    "warnings": warnings,
                    "threads": thread_summaries,
                    "note": "Use an exact cx_number with display_mode=evidence or llm to inspect one customer thread. Use run_llm=true for one-thread rerating.",
                }
            )

        if (display_mode == "llm" or run_llm) and not str(cx_number or "").strip():
            raise ValueError("display_mode=llm or run_llm=true requires an exact cx_number. Review one customer thread at a time.")

        selected_thread = thread_summaries[0] if thread_summaries else None
        timeline = build_whatsapp_thread_timeline(rows[:print_limit] if print_limit else rows, max_text_chars=max_text)
        result = {
            "status": "ok",
            "view": "llm" if display_mode == "llm" else "evidence",
            "title": "WhatsApp Conversation Thread",
            "query": query_payload,
            "message_count": len(rows),
            "thread_count": len(thread_summaries),
            "warnings": warnings,
            "thread": selected_thread,
            "timeline": timeline,
        }

        should_attach_llm = llm or display_mode == "llm" or run_llm
        if should_attach_llm and str(cx_number or "").strip() and selected_thread is not None:
            llm_context = build_whatsapp_thread_llm_context(
                admin_number=admin_number,
                cx_number=str(cx_number).strip(),
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                conversation_type=conversation_type,
                device=device,
                rows=rows[:print_limit] if print_limit else rows,
                max_text_chars=max_text,
            )
            result["llm_context"] = llm_context
            result["llm_prompt"] = build_whatsapp_thread_llm_prompt(llm_context)
            if run_llm:
                review_text = run_openai_prompt(
                    result["llm_prompt"],
                    model=model,
                    timeout_seconds=timeout_seconds,
                )
                result["llm_review"] = parse_whatsapp_thread_llm_review(review_text)
                result["llm_review_text"] = review_text

        return _encode(result)
    except Exception as exc:
        raise _api_error(exc) from exc
