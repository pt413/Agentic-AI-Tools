from __future__ import annotations

import time
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from .cache import ensure_booking_review_cache_table, mark_booking_review_stale
from .common import BOOKING_LLM_REVIEW_CACHE_TABLE, DEFAULT_MODEL, DEFAULT_SCHEMA, IST_OFFSET, build_in_params, compact_dict, now_ist_naive, q, q1, table_columns, table_exists, table_ref, today_ist
from .rating_runner import BookingCommunicationReviewRunner
import logging
logger = logging.getLogger(__name__)

def mark_bookings_stale(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    booking_ids: Sequence[int] | None = None,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    ensure_booking_review_cache_table(db, schema)
    ids = [int(value) for value in (booking_ids or []) if value not in (None, "")]
    for booking_id in ids:
        mark_booking_review_stale(db, schema, booking_id=booking_id, reason=reason or "booking evidence changed")
    return {"status": "ok", "count": len(ids), "booking_ids": ids, "reason": reason}


def list_stale_booking_ids(db: Session, schema: str = DEFAULT_SCHEMA, *, limit: int = 100) -> list[int]:
    ensure_booking_review_cache_table(db, schema)
    table = table_ref(schema, BOOKING_LLM_REVIEW_CACHE_TABLE)
    rows = q(
        db,
        f"""
        SELECT booking_id
        FROM {table}
        WHERE stale_at IS NOT NULL OR LOWER(COALESCE(status, '')) = 'stale'
        ORDER BY stale_at ASC NULLS FIRST, updated_at ASC NULLS FIRST
        LIMIT :limit_n
        """,
        {"limit_n": max(1, int(limit or 100))},
    )
    return [int(row["booking_id"]) for row in rows if row.get("booking_id") is not None]


def recompute_stale_bookings(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    limit: int = 50,
    customer_days: int = 30,
    model: str = DEFAULT_MODEL,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    booking_ids = list_stale_booking_ids(db, schema, limit=limit)
    runner = BookingCommunicationReviewRunner(db=db, schema=schema)
    reviews = []
    for booking_id in booking_ids:
        reviews.append(
            runner.review_one(
                booking_id=booking_id,
                customer_days=customer_days,
                run_llm=True,
                model=model,
                timeout_seconds=timeout_seconds,
                use_cache=True,
                force_refresh=True,
            )
        )
    return {"status": "ok", "requested": len(booking_ids), "booking_ids": booking_ids, "reviews": reviews}

# -----------------------------------------------------------------------------
# Active booking rating cron helpers
# -----------------------------------------------------------------------------

def _chunked(values: Sequence[int], size: int = 1000) -> list[list[int]]:
    items = [int(value) for value in values if value not in (None, "")]
    return [items[idx: idx + size] for idx in range(0, len(items), max(1, int(size or 1000)))]

def rate_bookings_with_new_communication(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    limit: int = 25,
    scan_limit: int = 5000,
    scan_batch_size: int = 100,
    activity_days: int = 2,
    include_contact_scans: bool = True,
    min_review_age_minutes: int = 300,
    customer_days: int = 30,
    model: str = DEFAULT_MODEL,
    timeout_seconds: int = 120,
    max_llm_messages: int = 12,
    max_llm_text_chars: int = 220,
    dry_run: bool = False,
    fail_fast: bool = False,
    debug: bool = False,
) -> dict[str, Any]:
    """Find and rate bookings that have new communication since their last review.

    Production behavior:
    - Prepare recent activity scan once.
    - Read IDs in pages, e.g. 100 at a time.
    - Skip recently-rated bookings.
    - Stop once `limit` eligible candidates are found.
    - Rate only those candidates.
    """
    from app.services.analytics_engine.capabilities.active_booking_activity_service import (
        ActiveBookingActivityService,
    )

    safe_limit = max(1, int(limit or 25))
    safe_scan_limit = max(safe_limit, int(scan_limit or 5000))
    safe_scan_batch_size = max(1, min(int(scan_batch_size or 100), safe_scan_limit))

    ensure_booking_review_cache_table(db, schema)
    started = time.perf_counter()
    activity_svc = ActiveBookingActivityService(db=db, schema=schema)
    activity_scan = activity_svc.prepare_id_scan(
        days=int(activity_days),
        debug=debug,
        include_contact_scans=bool(include_contact_scans),
    )
    cache_table = table_ref(schema, BOOKING_LLM_REVIEW_CACHE_TABLE)
    now_ist = now_ist_naive()
    candidates: list[int] = []
    scanned_ids: list[int] = []
    seen_scanned_ids: set[int] = set()

    skipped_recently_rated = 0
    page_count = 0
    offset = 0
    stopped_reason = "scan_exhausted"

    while len(candidates) < safe_limit and len(scanned_ids) < safe_scan_limit:
        remaining_scan = safe_scan_limit - len(scanned_ids)
        page_size = min(safe_scan_batch_size, remaining_scan)

        page_rows = activity_svc.final_id_page(
            limit=page_size,
            offset=offset,
            debug=debug,
        )
        page_count += 1
        logger.debug("Fetching activity page %d, offset=%d, rows=%d", page_count, offset, len(page_rows))

        if not page_rows:
            stopped_reason = "no_more_activity_rows"
            break

        offset += len(page_rows)

        page_ids: list[int] = []
        for row in page_rows:
            booking_id = row.get("booking_id") if isinstance(row, dict) else None
            if booking_id in (None, ""):
                continue

            booking_id = int(booking_id)
            if booking_id in seen_scanned_ids:
                continue

            seen_scanned_ids.add(booking_id)
            scanned_ids.append(booking_id)
            page_ids.append(booking_id)

        if not page_ids:
            if len(page_rows) < page_size:
                stopped_reason = "last_page_empty_after_dedupe"
                break
            continue

        in_sql, in_params = build_in_params(page_ids, "scanned")
        cache_rows = {
            int(row["booking_id"]): row
            for row in q(
                db,
                f"""
                SELECT booking_id, status, stale_at, review_generated_at
                FROM {cache_table}
                WHERE booking_id IN {in_sql}
                """,
                in_params,
            )
            if row.get("booking_id") is not None
        }

        for bid in page_ids:
            if len(candidates) >= safe_limit:
                stopped_reason = "target_reached"
                break

            cache_row = cache_rows.get(bid)

            if cache_row is None:
                candidates.append(bid)
                continue

            status = str(cache_row.get("status") or "").strip().lower()
            is_clean_ok = status == "ok" and cache_row.get("stale_at") in (None, "")

            if is_clean_ok:
                gen_at = cache_row.get("review_generated_at")
                if gen_at is not None:
                    if hasattr(gen_at, "tzinfo") and gen_at.tzinfo is not None:
                        gen_at_naive = gen_at.astimezone(
                            __import__("datetime").timezone(IST_OFFSET)
                        ).replace(tzinfo=None)
                    else:
                        gen_at_naive = gen_at

                    age_minutes = (now_ist - gen_at_naive).total_seconds() / 60
                    if age_minutes < min_review_age_minutes:
                        skipped_recently_rated += 1
                        continue

            candidates.append(bid)

        if len(page_rows) < page_size:
            stopped_reason = "last_page_reached"
            break

    if len(candidates) >= safe_limit:
        logger.info("Reached candidate limit (%d), stopping scan", safe_limit)
        stopped_reason = "target_reached"
    elif len(scanned_ids) >= safe_scan_limit:
        stopped_reason = "scan_limit_reached"

    if not scanned_ids:
        return {
            "status": "ok",
            "mode": "changed",
            "dry_run": dry_run,
            "activity_days": activity_days,
            "include_contact_scans": include_contact_scans,
            "limit": safe_limit,
            "scan_limit": safe_scan_limit,
            "scan_batch_size": safe_scan_batch_size,
            "page_count": page_count,
            "scanned_count": 0,
            "skipped_recently_rated_count": 0,
            "candidate_count": 0,
            "processed_count": 0,
            "stopped_reason": stopped_reason,
            "activity_scan": activity_scan if debug else None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    
    logger.info(
        "Changed-mode scan: scanned=%d, skipped_recently_rated=%d, limit=%d, candidates=%d min_review_age_minutes=%d",
        len(scanned_ids), skipped_recently_rated, safe_limit, len(candidates), min_review_age_minutes
    )
    if candidates:
        logger.info("Bookings to update (candidates): %s", candidates)

    if dry_run:
        return {
            "status": "ok",
            "mode": "changed",
            "dry_run": True,
            "activity_days": activity_days,
            "include_contact_scans": include_contact_scans,
            "limit": safe_limit,
            "scan_limit": safe_scan_limit,
            "scan_batch_size": safe_scan_batch_size,
            "page_count": page_count,
            "scanned_count": len(scanned_ids),
            "skipped_recently_rated_count": skipped_recently_rated,
            "candidate_count": len(candidates),
            "processed_count": 0,
            "booking_ids": candidates,
            "stopped_reason": stopped_reason,
            "activity_scan": activity_scan if debug else None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    runner = BookingCommunicationReviewRunner(db=db, schema=schema)
    reviews: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    ok_count = 0
    error_count = 0
    llm_count = 0

    for booking_id in candidates:
        logger.info("Updating booking %d (new communication detected)", booking_id)
        item_started = time.perf_counter()
        try:
            review = runner.review_one(
                booking_id=booking_id,
                customer_days=customer_days,
                run_llm=True,
                model=model,
                timeout_seconds=timeout_seconds,
                max_llm_messages=max_llm_messages,
                max_llm_text_chars=max_llm_text_chars,
                use_cache=False,
                force_refresh=True,
                include_context=False,
                include_prompt=False,
            )
            status = str(review.get("status") or "unknown")
            if status == "ok":
                ok_count += 1
            if review.get("error"):
                error_count += 1
            llm_count += 1
            row = compact_dict({
                "booking_id": review.get("booking_id") or booking_id,
                "status": status,
                "overall_score": review.get("overall_score"),
                "overall_priority_score": review.get("overall_priority_score"),
                "overall_risk": review.get("overall_risk"),
                "main_reason": review.get("main_reason"),
                "error": review.get("error"),
                "elapsed_ms": review.get("elapsed_ms"),
            })
            summary_rows.append(row)
            reviews.append(review if debug else row)
        except Exception as exc:
            error_count += 1
            err = {
                "booking_id": booking_id,
                "status": "error",
                "error": f"{exc.__class__.__name__}: {exc}",
                "elapsed_ms": round((time.perf_counter() - item_started) * 1000, 2),
            }
            summary_rows.append(err)
            reviews.append(err)
            if fail_fast:
                raise

    return {
        "status": "ok" if error_count == 0 else "partial_error",
        "mode": "changed",
        "dry_run": False,
        "activity_days": activity_days,
        "include_contact_scans": include_contact_scans,
        "limit": safe_limit,
        "scan_limit": safe_scan_limit,
        "scan_batch_size": safe_scan_batch_size,
        "page_count": page_count,
        "scanned_count": len(scanned_ids),
        "skipped_recently_rated_count": skipped_recently_rated,
        "candidate_count": len(candidates),
        "processed_count": len(reviews),
        "ok_count": ok_count,
        "error_count": error_count,
        "llm_count": llm_count,
        "booking_ids": candidates,
        "summary_rows": summary_rows,
        "reviews": reviews,
        "stopped_reason": stopped_reason,
        "activity_scan": activity_scan if debug else None,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }
       
def check_and_rate_single_booking(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    booking_id: int,
    activity_days: int = 2,
    customer_days: int = 30,
    model: str = DEFAULT_MODEL,
    timeout_seconds: int = 120,
    max_llm_messages: int = 12,
    max_llm_text_chars: int = 220,
    dry_run: bool = False,
    debug: bool = False,
) -> dict[str, Any]:
    """Check if a specific booking has new communication since its last review.

    If new communication found  → call LLM, update DB, return result.
    If no new communication     → return immediately with a clear message.

    How it detects new communication
    ---------------------------------
    1. Fetch review_generated_at from the cache table for this booking.
    2. Build the customer brief (CustomerBriefService) which collects the
       most recent message timestamp across ALL channels (WhatsApp, email,
       calls, tickets) into last_message_at.
    3. Compare: if last_message_at > review_generated_at → new communication.
       If no prior review exists → always rate.
    """
    from datetime import datetime, timezone as _tz

    from app.services.analytics_engine.capabilities.customer_brief_service import CustomerBriefService

    ensure_booking_review_cache_table(db, schema)
    started = time.perf_counter()
    safe_id = int(booking_id)
    cache_table = table_ref(schema, BOOKING_LLM_REVIEW_CACHE_TABLE)
    cache_row = q1(
        db,
        f"SELECT booking_id, status, review_generated_at FROM {cache_table} WHERE booking_id = :bid",
        {"bid": safe_id},
    )
    review_generated_at = cache_row.get("review_generated_at") if cache_row else None

    brief_svc = CustomerBriefService(db=db, schema=schema)
    brief = brief_svc.build(booking_id=safe_id, conversation_days=customer_days)
    llm_context = brief_svc.compact_for_llm(
        brief,
        max_messages=max_llm_messages,
        max_text_chars=max_llm_text_chars,
    )
    last_message_at_raw = (llm_context.get("conversation") or {}).get("last_message_at")

    def _to_ist_naive(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                return value.astimezone(_tz(IST_OFFSET)).replace(tzinfo=None)
            return value
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    return dt.astimezone(_tz(IST_OFFSET)).replace(tzinfo=None)
                return dt
            except Exception:
                return None
        return None

    last_msg_dt = _to_ist_naive(last_message_at_raw)
    review_dt = _to_ist_naive(review_generated_at)
    if last_msg_dt is None:
        has_new = False
    elif review_dt is None:
        has_new = True
    else:
        has_new = last_msg_dt > review_dt

    base_info = {
        "booking_id": safe_id,
        "last_message_at": str(last_message_at_raw) if last_message_at_raw else None,
        "review_generated_at": str(review_generated_at) if review_generated_at else None,
        "prior_cache_status": (cache_row.get("status") if cache_row else "missing"),
    }

    if not has_new:
        return {
            "status": "no_new_communication",
            "mode": "changed_single",
            "has_new_communication": False,
            "message": f"No new communication found for booking {safe_id}. Last message at {last_message_at_raw}, last review at {review_generated_at}.",
            **base_info,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    if dry_run:
        return {
            "status": "ok",
            "mode": "changed_single",
            "dry_run": True,
            "has_new_communication": True,
            "message": f"New communication found for booking {safe_id}. Would call LLM.",
            **base_info,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    runner = BookingCommunicationReviewRunner(db=db, schema=schema)
    review = runner.review_one(
        booking_id=safe_id,
        customer_days=customer_days,
        run_llm=True,
        model=model,
        timeout_seconds=timeout_seconds,
        max_llm_messages=max_llm_messages,
        max_llm_text_chars=max_llm_text_chars,
        use_cache=False,     
        force_refresh=True,
        include_context=False,
        include_prompt=False,
    )

    return {
        "status": review.get("status", "unknown"),
        "mode": "changed_single",
        "has_new_communication": True,
        **base_info,
        **compact_dict({
            "overall_score": review.get("overall_score"),
            "overall_priority_score": review.get("overall_priority_score"),
            "overall_risk": review.get("overall_risk"),
            "main_reason": review.get("main_reason"),
            "error": review.get("error"),
        }),
        "review": review if debug else None,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    
def list_active_booking_rows(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    limit: int = 5000,
    today: Any | None = None,
    only_rms_prop: bool = True,
) -> list[dict[str, Any]]:
    """Return active RMS bookings using the same active-stay definition as dashboard monitoring.

    Active means:
    - booking_status = 'success'
    - travel_from_date <= today <= travel_to_date
    - property is RMS Prop when staging_property_unit is available

    This function intentionally returns booking ids only plus compact context;
    it does not build customer brief or call the LLM.
    """
    table_name = "staging_booking_confirm"
    if not table_exists(db, schema, table_name):
        raise ValueError("staging_booking_confirm table is required for active booking rating cron")

    b_cols = table_columns(db, schema, table_name)
    required = {"booking_status", "travel_from_date", "travel_to_date"}
    missing = sorted(required - b_cols)
    if missing:
        raise ValueError(f"staging_booking_confirm missing required column(s): {', '.join(missing)}")
    if "source_id" not in b_cols and "booking_id" not in b_cols:
        raise ValueError("staging_booking_confirm requires source_id or booking_id")

    id_expr = "b.source_id" if "source_id" in b_cols else "b.booking_id"
    raw_booking_id_expr = "b.booking_id" if "booking_id" in b_cols else "NULL::bigint"
    user_id_expr = "b.user_id" if "user_id" in b_cols else "NULL::bigint"
    lead_id_expr = "b.lead_id" if "lead_id" in b_cols else "NULL::bigint"
    prop_id_expr = "b.prop_id" if "prop_id" in b_cols else "NULL::bigint"
    booking_type_expr = "b.booking_type" if "booking_type" in b_cols else "NULL::text"
    booking_dt_expr = "b.booking_datetime" if "booking_datetime" in b_cols else "NULL::timestamp"

    joins: list[str] = []
    select_property_cols = "NULL::text AS property_name, NULL::text AS building_id, NULL::text AS rms_prop"
    rms_filter = ""
    if "prop_id" in b_cols and table_exists(db, schema, "staging_property_unit"):
        pu_cols = table_columns(db, schema, "staging_property_unit")
        if "prop_id" in pu_cols:
            order_col = "last_updated_on" if "last_updated_on" in pu_cols else "synced_at" if "synced_at" in pu_cols else "prop_id"
            unit_name_expr = "unit_name::text" if "unit_name" in pu_cols else "NULL::text"
            building_expr = "building_id::text" if "building_id" in pu_cols else "NULL::text"
            rms_expr = "rms_prop::text" if "rms_prop" in pu_cols else "NULL::text"
            joins.append(
                f"""
                LEFT JOIN (
                    SELECT DISTINCT ON (prop_id::text)
                        prop_id::text AS prop_key,
                        {unit_name_expr} AS property_name,
                        {building_expr} AS building_id,
                        {rms_expr} AS rms_prop
                    FROM {table_ref(schema, 'staging_property_unit')}
                    WHERE prop_id IS NOT NULL
                    ORDER BY prop_id::text, {order_col} DESC NULLS LAST
                ) pu ON pu.prop_key = b.prop_id::text
                """
            )
            select_property_cols = "pu.property_name, pu.building_id, pu.rms_prop"
            if only_rms_prop and "rms_prop" in pu_cols:
                rms_filter = "AND LOWER(TRIM(COALESCE(pu.rms_prop::text, ''))) = 'rms prop'"

    params = {
        "today": today or today_ist(),
        "limit_n": max(1, int(limit or 5000)),
    }
    rows = q(
        db,
        f"""
        SELECT
            {id_expr}::bigint AS booking_id,
            {raw_booking_id_expr} AS raw_booking_id,
            {user_id_expr} AS user_id,
            {lead_id_expr} AS lead_id,
            {prop_id_expr} AS prop_id,
            {select_property_cols},
            b.booking_status AS booking_status,
            {booking_type_expr} AS booking_type,
            {booking_dt_expr} AS booking_datetime,
            b.travel_from_date AS travel_from_date,
            b.travel_to_date AS travel_to_date
        FROM {table_ref(schema, table_name)} b
        {' '.join(joins)}
        WHERE {id_expr} IS NOT NULL
          AND LOWER(TRIM(COALESCE(b.booking_status::text, ''))) = 'success'
          AND b.travel_from_date IS NOT NULL
          AND b.travel_to_date IS NOT NULL
          AND b.travel_from_date::date <= CAST(:today AS date)
          AND b.travel_to_date::date >= CAST(:today AS date)
          {rms_filter}
        ORDER BY b.travel_to_date ASC NULLS LAST, b.travel_from_date ASC NULLS LAST, {id_expr} DESC
        LIMIT :limit_n
        """,
        params,
    )
    return rows


def list_active_booking_ids(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    limit: int = 5000,
    today: Any | None = None,
    only_rms_prop: bool = True,
) -> list[int]:
    rows = list_active_booking_rows(db, schema, limit=limit, today=today, only_rms_prop=only_rms_prop)
    return [int(row["booking_id"]) for row in rows if row.get("booking_id") is not None]


def _cache_rows_by_booking_id(db: Session, schema: str, booking_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    ensure_booking_review_cache_table(db, schema)
    ids = [int(value) for value in booking_ids if value not in (None, "")]
    if not ids:
        return {}
    table = table_ref(schema, BOOKING_LLM_REVIEW_CACHE_TABLE)
    out: dict[int, dict[str, Any]] = {}
    for chunk_index, chunk in enumerate(_chunked(ids, 1000)):
        in_sql, in_params = build_in_params(chunk, f"bid{chunk_index}")
        rows = q(
            db,
            f"""
            SELECT booking_id, status, stale_at, updated_at, overall_score, overall_priority_score, overall_risk
            FROM {table}
            WHERE booking_id IN {in_sql}
            """,
            in_params,
        )
        for row in rows:
            if row.get("booking_id") is not None:
                out[int(row["booking_id"])] = row
    return out


def _booking_needs_rating(cache_row: dict[str, Any] | None, *, candidate_mode: str = "due", force_refresh: bool = False) -> bool:
    if force_refresh:
        return True
    normalized_mode = str(candidate_mode or "due").strip().lower()
    if normalized_mode in {"all", "active_all", "active-all"}:
        return True
    if cache_row is None:
        return True
    status = str(cache_row.get("status") or "").strip().lower()
    stale = cache_row.get("stale_at") not in (None, "")
    if normalized_mode == "missing":
        return False
    if normalized_mode == "stale":
        return stale or status == "stale"
    # Default: due = missing/stale/non-ok.
    return stale or status != "ok"


def list_active_booking_rating_candidates(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    scan_limit: int = 5000,
    limit: int = 100,
    today: Any | None = None,
    only_rms_prop: bool = True,
    candidate_mode: str = "due",
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Return active booking rows that should be rated by cron.

    candidate_mode:
    - due: missing cache, stale cache, or non-ok cache
    - missing: active bookings with no cache row
    - stale: active bookings marked stale
    - all: every active booking; ok rows still fast-hit unless force_refresh=True
    """
    active_rows = list_active_booking_rows(db, schema, limit=scan_limit, today=today, only_rms_prop=only_rms_prop)
    active_ids = [int(row["booking_id"]) for row in active_rows if row.get("booking_id") is not None]
    cache_map = _cache_rows_by_booking_id(db, schema, active_ids)

    normalized_mode = str(candidate_mode or "due").strip().lower()
    candidates: list[dict[str, Any]] = []
    skipped_ok = 0
    missing_count = 0
    stale_count = 0
    non_ok_count = 0
    for row in active_rows:
        booking_id = int(row["booking_id"])
        cache_row = cache_map.get(booking_id)
        if cache_row is None:
            missing_count += 1
        else:
            status = str(cache_row.get("status") or "").strip().lower()
            if cache_row.get("stale_at") not in (None, ""):
                stale_count += 1
            if status and status != "ok":
                non_ok_count += 1

        include = False
        if normalized_mode == "missing":
            include = cache_row is None or bool(force_refresh)
        else:
            include = _booking_needs_rating(cache_row, candidate_mode=normalized_mode, force_refresh=force_refresh)
        if include:
            merged = dict(row)
            merged["cache"] = cache_row or {"status": "missing"}
            candidates.append(merged)
        else:
            skipped_ok += 1

    capped = candidates[: max(1, int(limit or 100))]
    return {
        "status": "ok",
        "mode": normalized_mode,
        "active_booking_count": len(active_rows),
        "candidate_count": len(candidates),
        "returned_count": len(capped),
        "skipped_ok_count": skipped_ok,
        "missing_cache_count": missing_count,
        "stale_cache_count": stale_count,
        "non_ok_cache_count": non_ok_count,
        "truncated": len(candidates) > len(capped),
        "booking_ids": [int(row["booking_id"]) for row in capped if row.get("booking_id") is not None],
        "rows": capped,
    }


def rate_active_bookings(
    db: Session,
    schema: str = DEFAULT_SCHEMA,
    *,
    scan_limit: int = 5000,
    limit: int = 25,
    customer_days: int = 30,
    model: str = DEFAULT_MODEL,
    timeout_seconds: int = 120,
    max_llm_messages: int = 12,
    max_llm_text_chars: int = 220,
    candidate_mode: str = "due",
    force_refresh: bool = False,
    only_rms_prop: bool = True,
    dry_run: bool = False,
    sleep_seconds: float = 0.0,
    fail_fast: bool = False,
    debug: bool = False,
) -> dict[str, Any]:
    """Cron-safe active booking rating job.

    The default mode is safe for frequent cron runs:
    it scans all active bookings, rates only missing/stale/non-ok rows, and
    preserves ok cache rows. Use force_refresh=True only for deliberate backfill.
    """
    started = time.perf_counter()
    ensure_booking_review_cache_table(db, schema)
    candidate_payload = list_active_booking_rating_candidates(
        db,
        schema,
        scan_limit=scan_limit,
        limit=limit,
        only_rms_prop=only_rms_prop,
        candidate_mode=candidate_mode,
        force_refresh=force_refresh,
    )
    booking_ids = [int(value) for value in candidate_payload.get("booking_ids") or []]
    if dry_run:
        return {
            "status": "ok",
            "mode": "active_booking_rating_cron",
            "dry_run": True,
            "schema": schema,
            "candidate_mode": candidate_mode,
            "scan_limit": int(scan_limit),
            "limit": int(limit),
            "customer_days": int(customer_days),
            **{k: v for k, v in candidate_payload.items() if k != "rows"},
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    runner = BookingCommunicationReviewRunner(db=db, schema=schema)
    reviews: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    ok_count = 0
    error_count = 0
    cache_hit_count = 0
    llm_or_store_count = 0

    for index, booking_id in enumerate(booking_ids, start=1):
        item_started = time.perf_counter()
        try:
            review = runner.review_one(
                booking_id=booking_id,
                customer_days=customer_days,
                run_llm=True,
                model=model,
                timeout_seconds=timeout_seconds,
                max_llm_messages=max_llm_messages,
                max_llm_text_chars=max_llm_text_chars,
                use_cache=True,
                force_refresh=force_refresh,
                include_context=False,
                include_prompt=False,
            )
            status = str(review.get("status") or "unknown")
            if status == "ok":
                ok_count += 1
            if review.get("error"):
                error_count += 1
            if review.get("cached"):
                cache_hit_count += 1
            else:
                llm_or_store_count += 1
            compact_review = {
                "booking_id": review.get("booking_id") or booking_id,
                "status": status,
                "cached": review.get("cached"),
                "cache_status": review.get("cache_status"),
                "overall_score": review.get("overall_score"),
                "overall_priority_score": review.get("overall_priority_score"),
                "overall_risk": review.get("overall_risk"),
                "main_reason": review.get("main_reason"),
                "error": review.get("error"),
                "elapsed_ms": review.get("elapsed_ms"),
            }
            summary_rows.append(compact_dict(compact_review))
            if debug:
                reviews.append(review)
            else:
                reviews.append(compact_dict(compact_review))
        except Exception as exc:
            error_count += 1
            error_row = {
                "booking_id": booking_id,
                "status": "error",
                "error": f"{exc.__class__.__name__}: {exc}",
                "elapsed_ms": round((time.perf_counter() - item_started) * 1000, 2),
            }
            summary_rows.append(error_row)
            reviews.append(error_row)
            if fail_fast:
                raise
        if sleep_seconds and index < len(booking_ids):
            time.sleep(max(0.0, float(sleep_seconds)))

    return {
        "status": "ok" if error_count == 0 else "partial_error",
        "mode": "active_booking_rating_cron",
        "dry_run": False,
        "schema": schema,
        "candidate_mode": candidate_mode,
        "force_refresh": bool(force_refresh),
        "scan_limit": int(scan_limit),
        "limit": int(limit),
        "customer_days": int(customer_days),
        "active_booking_count": candidate_payload.get("active_booking_count"),
        "candidate_count": candidate_payload.get("candidate_count"),
        "requested_count": len(booking_ids),
        "processed_count": len(reviews),
        "ok_count": ok_count,
        "error_count": error_count,
        "cache_hit_count": cache_hit_count,
        "llm_or_store_count": llm_or_store_count,
        "truncated": candidate_payload.get("truncated"),
        "booking_ids": booking_ids,
        "summary_rows": summary_rows,
        "reviews": reviews,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }
