from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.services.analytics_engine.capabilities.booking_management.common import DEFAULT_MODEL, DEFAULT_SCHEMA, compact_dict
from app.services.analytics_engine.capabilities.booking_management.llm_client import run_openai_prompt, stable_json_hash
from app.services.analytics_engine.capabilities.staff_activity_common import now_ist_naive, phone_last10
from app.services.analytics_engine.capabilities.staff_activity_review import StaffActivityReviewService

from .cache import (
    STAFF_ACTION_INTELLIGENCE_CONTEXT_VERSION,
    get_staff_action_review_by_id,
    get_staff_action_review,
    store_staff_action_review,
)
from .prompt import (
    build_staff_action_assistant_prompt,
    build_staff_action_intelligence_prompt,
    parse_staff_action_assistant_json,
    parse_staff_action_llm_json,
)
from .rules import ACTION_OWNER_BY_ROLE, build_heuristic_review, compute_business_signals


ASSISTANT_SECTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "calls": ("call", "calls", "phone", "dial", "callback", "missed", "ring", "talk time", "duration", "connected"),
    "whatsapp": ("whatsapp", "chat", "message", "messages", "reply", "text"),
    "tickets": ("ticket", "tickets", "sla", "support", "complaint", "issue", "closure", "reopen"),
    "finance_rows": ("finance", "payment", "payments", "invoice", "pending", "balance", "refund", "rent", "utr", "charge"),
    "leads": ("lead", "leads", "pipeline", "follow up", "follow-up", "enquiry", "enquiry"),
    "bookings": ("booking", "bookings", "conversion", "converted", "close", "closure"),
    "travel_cart": ("travel cart", "cart", "attempt", "attempts"),
    "site_visits": ("site visit", "site visits", "visit", "visits", "field"),
    "timeline": ("timeline", "history", "sequence", "when", "latest", "recent"),
    "rating": ("rating", "score", "priority", "risk", "why", "reason", "problem", "issue", "wrong"),
}
TIMELINE_CHANNELS_BY_SECTION = {
    "calls": {"call"},
    "whatsapp": {"whatsapp"},
    "tickets": {"ticket"},
    "finance_rows": {"finance"},
    "leads": {"lead"},
    "bookings": {"booking"},
    "travel_cart": {"travel_cart"},
    "site_visits": {"site_visit"},
}


def _staff_key_from_profile(staff: dict[str, Any]) -> str:
    username = str(staff.get("username") or "").strip()
    if username:
        return username
    email = str(staff.get("email") or "").strip().lower()
    if email:
        return email
    phone = phone_last10(staff.get("phone")) or phone_last10(staff.get("normalized_phone")) or phone_last10(staff.get("phone_number"))
    if phone:
        return phone
    raise ValueError("Could not derive staff_key from resolved staff profile.")


def _normalize_risk(value: Any) -> str:
    text_value = str(value or "").strip().lower()
    if text_value == "high":
        return "High"
    if text_value == "medium":
        return "Medium"
    if text_value == "low":
        return "Low"
    return "No Data"


def _normalize_actions(actions: Any, *, fallback_owner: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in actions or []:
        if not isinstance(row, dict):
            continue
        action_text = str(row.get("action") or "").strip()
        if not action_text:
            continue
        normalized.append(
            compact_dict(
                {
                    "owner": row.get("owner") or fallback_owner,
                    "action": action_text,
                    "priority": row.get("priority") or "Medium",
                    "due_by": row.get("due_by") or "Next review window",
                    "due_by_at": row.get("due_by_at"),
                    "evidence": row.get("evidence") or "",
                    "status": str(row.get("status") or "open").strip().lower() or "open",
                }
            )
        )
    return normalized


def _merge_llm_review(
    *,
    heuristic_review: dict[str, Any],
    llm_review: dict[str, Any],
    role_scope: str,
) -> dict[str, Any]:
    merged = deepcopy(heuristic_review)
    if not isinstance(llm_review, dict):
        return merged

    fallback_owner = ACTION_OWNER_BY_ROLE.get(role_scope, ACTION_OWNER_BY_ROLE["generic"])
    llm_staff = llm_review.get("staff") if isinstance(llm_review.get("staff"), dict) else {}
    llm_rating = llm_review.get("rating") if isinstance(llm_review.get("rating"), dict) else {}
    llm_business_impact = llm_review.get("business_impact") if isinstance(llm_review.get("business_impact"), dict) else {}
    llm_findings = [str(item).strip() for item in (llm_review.get("key_findings") or []) if str(item).strip()]
    llm_actions = _normalize_actions(llm_review.get("recommended_actions"), fallback_owner=fallback_owner)
    llm_coaching = [str(item).strip() for item in (llm_review.get("coaching_points") or []) if str(item).strip()]
    llm_gaps = [str(item).strip() for item in (llm_review.get("data_gaps") or []) if str(item).strip()]

    merged_staff = merged.get("staff") if isinstance(merged.get("staff"), dict) else {}
    merged_staff.update(compact_dict({"username": llm_staff.get("username"), "team": llm_staff.get("team"), "role_scope": llm_staff.get("role_scope")}))
    merged["staff"] = merged_staff

    merged_rating = merged.get("rating") if isinstance(merged.get("rating"), dict) else {}
    if llm_rating.get("overall_score") not in (None, ""):
        merged_rating["overall_score"] = llm_rating.get("overall_score")
    if llm_rating.get("priority_score") not in (None, ""):
        merged_rating["priority_score"] = llm_rating.get("priority_score")
    if llm_rating.get("reason") not in (None, ""):
        merged_rating["reason"] = llm_rating.get("reason")
    merged_rating["risk"] = _normalize_risk(llm_rating.get("risk") or merged_rating.get("risk"))
    merged["rating"] = merged_rating

    if llm_business_impact:
        merged["business_impact"] = compact_dict(
            {
                "customer_risk": llm_business_impact.get("customer_risk"),
                "revenue_risk": llm_business_impact.get("revenue_risk"),
                "operation_risk": llm_business_impact.get("operation_risk"),
                "trust_risk": llm_business_impact.get("trust_risk"),
            }
        ) or merged.get("business_impact") or {}
    if llm_findings:
        merged["key_findings"] = llm_findings
    if llm_actions:
        merged["recommended_actions"] = llm_actions
    if llm_coaching:
        merged["coaching_points"] = llm_coaching

    existing_gaps = [str(item).strip() for item in (merged.get("data_gaps") or []) if str(item).strip()]
    merged["data_gaps"] = list(dict.fromkeys(existing_gaps + llm_gaps))

    risk_text = str((merged.get("rating") or {}).get("risk") or "").strip().lower()
    if risk_text in {"high", "medium"} and not merged.get("recommended_actions"):
        merged["recommended_actions"] = heuristic_review.get("recommended_actions") or []

    return merged


def select_assistant_sections(question: str) -> list[str]:
    text_value = str(question or "").strip().lower()
    sections: list[str] = []
    for section_name, keywords in ASSISTANT_SECTION_KEYWORDS.items():
        if any(keyword in text_value for keyword in keywords):
            sections.append(section_name)
    if not sections:
        sections = ["rating", "timeline"]
    if "rating" not in sections:
        sections.insert(0, "rating")
    non_rating_sections = [section_name for section_name in sections if section_name != "rating"]
    if not non_rating_sections:
        sections.append("timeline")
    return list(dict.fromkeys(sections))


def _activity_seed_kwargs(review: dict[str, Any]) -> dict[str, Any]:
    staff = review.get("staff") if isinstance(review.get("staff"), dict) else {}
    if staff.get("username"):
        return {"username": staff.get("username")}
    if staff.get("email"):
        return {"email": staff.get("email")}
    if staff.get("phone"):
        return {"phone": staff.get("phone")}
    raise ValueError("Selected staff review does not contain username, email, or phone for assistant lookup.")


def _slice_rows(rows: Any, *, limit: int) -> list[dict[str, Any]]:
    return [row for row in list(rows or [])[:limit] if isinstance(row, dict)]


def _select_review_for_question(review: dict[str, Any], sections: list[str]) -> dict[str, Any]:
    return compact_dict(
        {
            "staff": review.get("staff"),
            "rating": review.get("rating"),
            "business_impact": review.get("business_impact"),
            "key_findings": review.get("key_findings"),
            "recommended_actions": review.get("recommended_actions"),
            "coaching_points": review.get("coaching_points"),
            "data_gaps": review.get("data_gaps"),
            "evidence_counts": review.get("evidence_counts"),
            "focus_for_question": sections,
        }
    )


def _build_assistant_context(
    *,
    review: dict[str, Any],
    activity: dict[str, Any],
    signals: dict[str, Any],
    question: str,
) -> tuple[list[str], dict[str, Any]]:
    sections = select_assistant_sections(question)
    data = activity.get("data") if isinstance(activity.get("data"), dict) else {}
    timeline = [row for row in (activity.get("timeline") or []) if isinstance(row, dict)]
    selected_data: dict[str, Any] = {}

    for section_name in sections:
        if section_name == "rating":
            continue
        if section_name == "timeline":
            selected_data["timeline"] = _slice_rows(timeline, limit=30)
            continue
        if section_name in data:
            selected_data[section_name] = _slice_rows(data.get(section_name), limit=60)
            if "timeline" not in selected_data and section_name in TIMELINE_CHANNELS_BY_SECTION:
                allowed_channels = TIMELINE_CHANNELS_BY_SECTION.get(section_name) or set()
                selected_data["timeline"] = _slice_rows(
                    [row for row in timeline if str(row.get("channel") or "").strip().lower() in allowed_channels],
                    limit=25,
                )

    if len(selected_data) <= 1:
        problem_sections = []
        if int(signals.get("unrecovered_missed_calls_count") or 0) > 0:
            problem_sections.append("calls")
        if int(signals.get("whatsapp_response_gap_count") or 0) > 0:
            problem_sections.append("whatsapp")
        if int(signals.get("ticket_sla_breach_count") or 0) > 0:
            problem_sections.append("tickets")
        if int(signals.get("finance_pending_rows") or 0) > 0:
            problem_sections.append("finance_rows")
        for section_name in problem_sections[:2]:
            if section_name not in selected_data and section_name in data:
                selected_data[section_name] = _slice_rows(data.get(section_name), limit=30)

    context_payload = compact_dict(
        {
            "question": question,
            "selected_sections": sections,
            "review_summary": _select_review_for_question(review, sections),
            "deterministic_signals": signals,
            "selected_activity_data": selected_data,
            "window": activity.get("window"),
        }
    )
    return sections, context_payload


class StaffActionIntelligenceService:
    def __init__(self, db: Session, schema: str = DEFAULT_SCHEMA) -> None:
        self.db = db
        self.schema = schema or DEFAULT_SCHEMA
        self.activity_service = StaffActivityReviewService(db=db, schema=self.schema)

    def _cached_review_if_usable(
        self,
        *,
        staff_key: str,
        role_scope: str,
        days: int,
        force_refresh: bool,
        should_run_llm: bool,
    ) -> Optional[dict[str, Any]]:
        if force_refresh:
            return None
        cached = get_staff_action_review(self.db, self.schema, staff_key=staff_key, role_scope=role_scope, window_days=days)
        if not cached:
            return None
        if should_run_llm and not cached.get("llm_output"):
            return None
        return compact_dict({"view": "staff_action_intelligence_review", "cached": True, **cached})

    def build_review(
        self,
        *,
        username: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        role: str = "auto",
        days: int = 7,
        limit: int = 10000,
        print_limit: int = 50,
        max_text: int = 160,
        run_llm: bool = False,
        force_refresh: bool = False,
        model: str = DEFAULT_MODEL,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        resolved_staff = self.activity_service.resolve_staff(username=username, email=email, phone=phone)
        role_scope, _requested_scope, _resolution_source = self.activity_service._resolve_review_role_scope(
            staff=resolved_staff,
            requested_role=role or "auto",
        )
        staff_key = _staff_key_from_profile(resolved_staff)
        safe_days = int(days or 7)
        should_run_llm = bool(run_llm or force_refresh)

        cached = self._cached_review_if_usable(
            staff_key=staff_key,
            role_scope=role_scope,
            days=safe_days,
            force_refresh=force_refresh,
            should_run_llm=should_run_llm,
        )
        if cached:
            return cached

        activity = self.activity_service.build_staff_activity(
            username=username,
            email=email,
            phone=phone,
            role=role or "auto",
            days=safe_days,
            limit=limit,
            print_limit=print_limit,
            max_text=max_text,
            llm=True,
            display_mode="raw",
        )
        signals = compute_business_signals(activity)
        heuristic_review = build_heuristic_review(activity, signals, llm_requested=should_run_llm)
        final_review = deepcopy(heuristic_review)
        llm_output: dict[str, Any] = {}
        llm_prompt = build_staff_action_intelligence_prompt(
            activity=activity,
            signals=signals,
            heuristic_review=heuristic_review,
        )
        llm_error: Optional[str] = None

        if should_run_llm:
            try:
                llm_text = run_openai_prompt(llm_prompt, model=model, timeout_seconds=timeout_seconds)
                parsed_review = parse_staff_action_llm_json(llm_text)
                llm_output = {"raw_text": llm_text, "parsed": parsed_review}
                final_review = _merge_llm_review(
                    heuristic_review=heuristic_review,
                    llm_review=parsed_review,
                    role_scope=role_scope,
                )
            except Exception as exc:
                llm_error = f"{exc.__class__.__name__}: {exc}"
                gaps = [str(item).strip() for item in (final_review.get("data_gaps") or []) if str(item).strip()]
                gaps.append(f"LLM review failed; deterministic review kept instead. Error: {llm_error}")
                final_review["data_gaps"] = list(dict.fromkeys(gaps))

        rated_at = now_ist_naive()
        source_context = {
            "staff": activity.get("staff"),
            "window": activity.get("window"),
            "role_scope": role_scope,
            "counts": activity.get("counts"),
            "timeline": activity.get("timeline"),
            "signals": signals,
            "llm_context": activity.get("llm_context") or {},
        }
        context_hash = stable_json_hash(source_context)
        payload = {
            "staff_key": staff_key,
            "username": (activity.get("staff") or {}).get("username"),
            "email": (activity.get("staff") or {}).get("email"),
            "phone": (activity.get("staff") or {}).get("phone"),
            "team": (activity.get("staff") or {}).get("team"),
            "role_scope": role_scope,
            "window_days": safe_days,
            "window_start": (activity.get("window") or {}).get("start"),
            "window_end": (activity.get("window") or {}).get("end"),
            "overall_score": ((final_review.get("rating") or {}).get("overall_score")),
            "priority_score": ((final_review.get("rating") or {}).get("priority_score")),
            "risk": ((final_review.get("rating") or {}).get("risk")),
            "reason": ((final_review.get("rating") or {}).get("reason")),
            "status": None,
            "business_impact_json": final_review.get("business_impact") or {},
            "key_findings_json": final_review.get("key_findings") or [],
            "recommended_actions_json": final_review.get("recommended_actions") or [],
            "coaching_points_json": final_review.get("coaching_points") or [],
            "data_gaps_json": final_review.get("data_gaps") or [],
            "evidence_counts_json": final_review.get("evidence_counts") or {},
            "llm_output_json": llm_output,
            "source_context_json": source_context,
            "model": model if should_run_llm else None,
            "context_version": STAFF_ACTION_INTELLIGENCE_CONTEXT_VERSION,
            "context_hash": context_hash,
            "error": llm_error,
            "rated_at": rated_at,
        }
        stored = store_staff_action_review(self.db, self.schema, payload=payload)
        return compact_dict(
            {
                "view": "staff_action_intelligence_review",
                "cached": False,
                "heuristics": {
                    "signals": signals,
                    "review": heuristic_review,
                },
                "llm_requested": should_run_llm,
                "llm_prompt": llm_prompt,
                "llm_context": activity.get("llm_context") or {},
                **stored,
            }
        )

    def answer_question(
        self,
        *,
        question: str,
        review_id: Optional[int] = None,
        username: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        role: str = "auto",
        days: int = 7,
        limit: int = 10000,
        print_limit: int = 50,
        max_text: int = 160,
        model: str = DEFAULT_MODEL,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        question_text = str(question or "").strip()
        if not question_text:
            raise ValueError("question is required for the staff assistant.")

        review: Optional[dict[str, Any]] = None
        seed_kwargs: dict[str, Any] = {}
        safe_days = int(days or 7)

        if review_id not in (None, ""):
            review = get_staff_action_review_by_id(self.db, self.schema, review_id=int(review_id))
            if not review:
                raise ValueError("Staff action intelligence review not found for assistant question.")
            seed_kwargs = _activity_seed_kwargs(review)
            window = review.get("window") if isinstance(review.get("window"), dict) else {}
            safe_days = int(window.get("days") or safe_days)
        else:
            if username:
                seed_kwargs = {"username": username}
            elif email:
                seed_kwargs = {"email": email}
            elif phone:
                seed_kwargs = {"phone": phone}
            else:
                raise ValueError("Provide review_id or one of username/email/phone for the staff assistant.")
            review = self.build_review(
                days=safe_days,
                role=role,
                limit=limit,
                print_limit=print_limit,
                max_text=max_text,
                run_llm=False,
                force_refresh=False,
                **seed_kwargs,
            )

        activity = self.activity_service.build_staff_activity(
            role=role or "auto",
            days=safe_days,
            limit=limit,
            print_limit=print_limit,
            max_text=max_text,
            llm=False,
            display_mode="raw",
            **seed_kwargs,
        )
        signals = compute_business_signals(activity)
        if not review:
            heuristic_review = build_heuristic_review(activity, signals, llm_requested=False)
            review = {
                "staff": heuristic_review.get("staff") or activity.get("staff"),
                "rating": heuristic_review.get("rating") or {},
                "business_impact": heuristic_review.get("business_impact") or {},
                "key_findings": heuristic_review.get("key_findings") or [],
                "recommended_actions": heuristic_review.get("recommended_actions") or [],
                "coaching_points": heuristic_review.get("coaching_points") or [],
                "data_gaps": heuristic_review.get("data_gaps") or [],
                "evidence_counts": heuristic_review.get("evidence_counts") or {},
            }

        sections, context_payload = _build_assistant_context(
            review=review,
            activity=activity,
            signals=signals,
            question=question_text,
        )
        prompt = build_staff_action_assistant_prompt(
            question=question_text,
            context_payload=context_payload,
        )
        llm_text = run_openai_prompt(prompt, model=model, timeout_seconds=timeout_seconds)
        parsed = parse_staff_action_assistant_json(llm_text)

        return compact_dict(
            {
                "view": "staff_action_assistant_answer",
                "question": question_text,
                "review_id": review.get("id"),
                "staff": review.get("staff"),
                "rating": review.get("rating"),
                "used_sections": parsed.get("used_sections") or sections,
                "answer": parsed.get("answer"),
                "evidence": parsed.get("evidence") or [],
                "data_gaps": parsed.get("data_gaps") or review.get("data_gaps") or [],
                "selected_sections": sections,
            }
        )

    def batch_rate(
        self,
        *,
        team: Optional[str] = None,
        active: bool = True,
        days: int = 7,
        limit: int = 100,
        run_llm: bool = False,
        force_refresh: bool = False,
        fail_fast: bool = False,
        model: str = DEFAULT_MODEL,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        staff_rows = self.activity_service.list_staff(team=team, active=active, limit=limit)
        processed = 0
        saved = 0
        failures: list[dict[str, Any]] = []
        reviews: list[dict[str, Any]] = []

        for row in staff_rows:
            if not isinstance(row, dict):
                continue
            processed += 1
            review_kwargs = {}
            if row.get("username"):
                review_kwargs["username"] = row.get("username")
            elif row.get("email"):
                review_kwargs["email"] = row.get("email")
            elif row.get("phone") or row.get("phone_number") or row.get("normalized_phone"):
                review_kwargs["phone"] = row.get("phone") or row.get("phone_number") or row.get("normalized_phone")
            else:
                failures.append({"staff": row, "error": "Missing username, email, and phone seed."})
                if fail_fast:
                    break
                continue

            try:
                review = self.build_review(
                    days=days,
                    run_llm=run_llm,
                    force_refresh=force_refresh,
                    model=model,
                    timeout_seconds=timeout_seconds,
                    **review_kwargs,
                )
                reviews.append(review)
                saved += 1
            except Exception as exc:
                try:
                    self.db.rollback()
                except Exception:
                    pass
                failures.append(
                    {
                        "staff": compact_dict(
                            {
                                "username": row.get("username"),
                                "email": row.get("email"),
                                "phone": row.get("phone") or row.get("phone_number") or row.get("normalized_phone"),
                                "team": row.get("team"),
                            }
                        ),
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
                )
                if fail_fast:
                    break

        return {
            "view": "staff_action_intelligence_batch_rate",
            "team": team,
            "active": active,
            "days": int(days or 7),
            "limit": int(limit),
            "run_llm": bool(run_llm),
            "force_refresh": bool(force_refresh),
            "processed": processed,
            "saved": saved,
            "failed": len(failures),
            "failures": failures,
            "reviews": reviews,
        }
