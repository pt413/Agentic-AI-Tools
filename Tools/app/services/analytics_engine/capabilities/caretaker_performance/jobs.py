from __future__ import annotations

import time
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_engine.capabilities.staff_activity_review import StaffActivityReviewService

from .cache import (
    ensure_caretaker_performance_cache_table,
    mark_caretaker_performance_stale,
    make_cache_key,
)
from .rating_runner import (
    CARETAKER_PERFORMANCE_DEFAULT_DAYS,
    CaretakerPerformanceReviewRunner,
)

DEFAULT_SCHEMA = "AnalyticsEngine"


def list_caretaker_candidates(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    active: bool = True,
    limit: int = 100,
) -> list[dict[str, Any]]:
    service = StaffActivityReviewService(db=db, schema=schema)
    return service.list_staff(team="Caretaker", active=active, limit=limit)


def _staff_identity_from_candidate(staff: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": staff.get("username"),
        "email": staff.get("email"),
        "phone": staff.get("phone") or staff.get("phone_number") or staff.get("normalized_phone"),
    }


def _is_error_status(result: dict[str, Any]) -> bool:
    status = str(result.get("status") or "").strip().lower()
    return status == "error" or bool(result.get("error"))


def _is_llm_called(result: dict[str, Any]) -> bool:
    return bool(result.get("llm_called"))


def _cache_status(result: dict[str, Any]) -> str:
    return str(result.get("cache_status") or "").strip().lower()


def rate_caretakers_batch(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    days: int = CARETAKER_PERFORMANCE_DEFAULT_DAYS,
    active: bool = True,
    limit: int = 500,
    model: str = "gpt-5-mini",
    timeout_seconds: int = 120,
    force_refresh: bool = False,
    run_llm: bool = False,
    sleep_seconds: float = 0.0,
    fail_fast: bool = False,
) -> dict[str, Any]:
    """
    Batch caretaker performance rating.

    Production behavior:
    - Lists active caretakers only by default.
    - Uses a fixed 30-day window by default.
    - Calls LLM only when cache is missing, force_refresh=True, or current
      30-day evidence/context differs from cached context_hash.
    - Dashboard can still show inactive caretakers if they already have valid
      cached 30-day ratings; this batch job does not delete inactive cache rows.
    """
    started = time.perf_counter()
    safe_days = int(days or CARETAKER_PERFORMANCE_DEFAULT_DAYS)

    ensure_caretaker_performance_cache_table(db, schema)

    candidates = list_caretaker_candidates(
        db,
        schema,
        active=active,
        limit=limit,
    )

    runner = CaretakerPerformanceReviewRunner(db=db, schema=schema)

    results: list[dict[str, Any]] = []

    ok_count = 0
    error_count = 0
    cache_hit_count = 0
    stale_count = 0
    miss_count = 0
    refreshed_count = 0
    llm_call_count = 0
    skipped_count = 0

    for index, staff in enumerate(candidates, start=1):
        item_started = time.perf_counter()
        identity = _staff_identity_from_candidate(staff)

        username = identity.get("username")
        email = identity.get("email")
        phone = identity.get("phone")

        try:
            result = runner.review_one(
                username=username,
                email=email,
                phone=phone,
                days=safe_days,
                model=model,
                timeout_seconds=timeout_seconds,
                run_llm=run_llm,
                use_cache=True,
                force_refresh=force_refresh,
                include_activity=False,
                include_prompt=False,
            )

            cache_status = _cache_status(result)
            llm_called = _is_llm_called(result)
            is_error = _is_error_status(result)

            if is_error:
                error_count += 1
            else:
                ok_count += 1

            if llm_called:
                llm_call_count += 1
                if not is_error:
                    refreshed_count += 1
            else:
                skipped_count += 1

            if cache_status in {"hit", "hit_unchanged"}:
                cache_hit_count += 1
            elif cache_status in {"stale", "stale_context_changed"}:
                stale_count += 1
            elif cache_status in {"miss", "not_found"}:
                miss_count += 1

            results.append(
                {
                    "index": index,
                    "username": username,
                    "email": email,
                    "phone": phone,
                    "status": result.get("status"),
                    "cached": result.get("cached"),
                    "cache_status": result.get("cache_status"),
                    "llm_called": llm_called,
                    "overall_score": result.get("overall_score"),
                    "priority_score": result.get("priority_score"),
                    "overall_risk": result.get("overall_risk"),
                    "main_reason": result.get("main_reason"),
                    "error": result.get("error"),
                    "elapsed_ms": round((time.perf_counter() - item_started) * 1000, 2),
                }
            )

        except Exception as exc:
            error_count += 1
            row = {
                "index": index,
                "username": username,
                "email": email,
                "phone": phone,
                "status": "error",
                "cached": False,
                "cache_status": "exception",
                "llm_called": False,
                "error": f"{exc.__class__.__name__}: {exc}",
                "elapsed_ms": round((time.perf_counter() - item_started) * 1000, 2),
            }
            results.append(row)

            if fail_fast:
                raise

        if sleep_seconds and index < len(candidates):
            time.sleep(max(0.0, float(sleep_seconds)))

    return {
        "status": "ok" if error_count == 0 else "partial_error",
        "mode": "caretaker_performance_batch_rating",
        "schema": schema,
        "days": safe_days,
        "active": active,
        "limit": int(limit),
        "requested": len(candidates),
        "processed": len(results),
        "ok_count": ok_count,
        "error_count": error_count,
        "cache_hit_count": cache_hit_count,
        "stale_count": stale_count,
        "miss_count": miss_count,
        "skipped_count": skipped_count,
        "refreshed_count": refreshed_count,
        "llm_call_count": llm_call_count,
        "force_refresh": force_refresh,
        "run_llm": run_llm,
        "results": results,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def mark_caretaker_stale_by_identity(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    username: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    days: int = CARETAKER_PERFORMANCE_DEFAULT_DAYS,
    reason: str = "caretaker evidence changed",
) -> dict[str, Any]:
    cache_key = make_cache_key(
        username=username,
        phone=phone,
        email=email,
        days=int(days or CARETAKER_PERFORMANCE_DEFAULT_DAYS),
    )
    return mark_caretaker_performance_stale(
        db,
        schema,
        cache_key=cache_key,
        reason=reason,
    )
