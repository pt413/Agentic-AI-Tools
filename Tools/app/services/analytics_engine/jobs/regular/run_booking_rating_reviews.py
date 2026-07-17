#!/usr/bin/env python3
"""
run_booking_rating_reviews.py

Cron-safe booking rating job for the Booking Handling Dashboard.

Purpose:
  - Find active RMS Prop bookings.
  - Rate missing/stale/non-ok booking_communication_review cache rows.
  - Keep dashboard fast because UI reads cache only and never calls LLM per row.

Typical commands:
  python -m app.services.analytics_engine.jobs.regular.run_booking_rating_reviews --mode active --limit 25 --pretty
  python -m app.services.analytics_engine.jobs.regular.run_booking_rating_reviews --mode stale --limit 25 --pretty
  python -m app.services.analytics_engine.jobs.regular.run_booking_rating_reviews --mode active --dry-run --pretty

Parallel behavior:
  - Defaults to 5 workers for active/missing/active-all/stale.
  - Use --workers 1 for the old sequential behavior.

Windows Task Scheduler example:
  Program:  C:\\BP_AI\\BP_AI\\venv\\Scripts\\python.exe
  Arguments: -m app.services.analytics_engine.jobs.regular.run_booking_rating_reviews --mode active --limit 25 --pretty
  Start in: C:\\BP_AI\\BP_AI\\FastApi

Linux cron example:
  */30 * * * * cd /opt/BP_AI/FastApi && /opt/BP_AI/venv/bin/python -m app.services.analytics_engine.jobs.regular.run_booking_rating_reviews --mode active --limit 25 >> /var/log/booking_rating_reviews.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import inspect
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


# -----------------------------------------------------------------------------
# Repo bootstrap
# -----------------------------------------------------------------------------

def _project_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent, *current.parents]:
        if (parent / "app").is_dir():
            return parent
    return Path.cwd()


PROJECT_ROOT = _project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.analytics_engine.capabilities.booking_management.common import (  # noqa: E402
    DEFAULT_MODEL,
    DEFAULT_SCHEMA,
    json_dumps,
)
from app.services.analytics_engine.capabilities.booking_management.jobs import (  # noqa: E402
    check_and_rate_single_booking,
    list_active_booking_rating_candidates,
    list_stale_booking_ids,
    rate_active_bookings,
    rate_bookings_with_new_communication,
    recompute_stale_bookings,
)


def _env_int(*names: str, default: int) -> int:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            try:
                return max(1, int(value))
            except Exception:
                pass
    return max(1, int(default))


DEFAULT_WORKERS = _env_int("BOOKING_RATING_WORKERS", "RATING_REVIEW_WORKERS", default=5)


def _try_load_env() -> None:
    if load_dotenv is None:
        return
    for env_path in (PROJECT_ROOT / ".env", PROJECT_ROOT.parent / ".env", Path.cwd() / ".env", Path.cwd().parent / ".env"):
        if env_path.exists():
            load_dotenv(env_path, override=False)


def _db_url(explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return explicit
    value = os.getenv("DATABASE_URL") or os.getenv("PG_URL")
    if value:
        return value
    _try_load_env()
    return os.getenv("DATABASE_URL") or os.getenv("PG_URL")


@contextmanager
def open_db_session(database_url: Optional[str] = None) -> Iterable[Session]:
    """Open DB session using repo SessionLocal first, then DATABASE_URL/PG_URL."""
    if not database_url:
        try:
            from app.db.database import SessionLocal  # type: ignore

            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()
            return
        except Exception:
            # Fall back to env URL below.
            pass

    url = _db_url(database_url)
    if not url:
        raise RuntimeError("No DB available. Run inside repo or set DATABASE_URL / PG_URL, or pass --database-url.")
    engine = create_engine(url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        try:
            db.close()
        finally:
            engine.dispose()


def _clean_payload_for_stdout(payload: dict[str, Any], *, debug: bool) -> dict[str, Any]:
    if debug:
        return payload
    clean = dict(payload)
    # Keep stdout compact for cron logs. Full LLM review text is already stored in DB cache.
    for review in clean.get("reviews") or []:
        if isinstance(review, dict):
            review.pop("review_text", None)
            review.pop("llm_prompt", None)
            review.pop("llm_context", None)
    return clean



def _compact_summary_row(review: dict[str, Any]) -> dict[str, Any]:
    """Small per-booking row for cron stdout."""
    return {
        key: value
        for key, value in {
            "booking_id": review.get("booking_id"),
            "status": review.get("status"),
            "cached": review.get("cached"),
            "cache_status": review.get("cache_status"),
            "overall_score": review.get("overall_score") or review.get("score"),
            "overall_priority_score": review.get("overall_priority_score") or review.get("priority_score"),
            "overall_risk": review.get("overall_risk") or review.get("risk"),
            "main_reason": review.get("main_reason"),
            "elapsed_ms": review.get("elapsed_ms"),
            "error": review.get("error"),
        }.items()
        if value not in (None, "", [], {})
    }


def _safe_review_one_call(runner: Any, **kwargs: Any) -> dict[str, Any]:
    """Call review_one while tolerating small signature differences across repo versions."""
    sig = inspect.signature(runner.review_one)
    accepted = {key: value for key, value in kwargs.items() if key in sig.parameters}
    return runner.review_one(**accepted)


def _rate_one_booking_worker(
    *,
    database_url: Optional[str],
    schema: str,
    booking_id: int,
    customer_days: int,
    model: str,
    timeout_seconds: int,
    max_llm_messages: int,
    max_llm_text_chars: int,
    force_refresh: bool,
    debug: bool,
) -> dict[str, Any]:
    """Rate exactly one booking in its own DB session.

    Important: SQLAlchemy sessions are not thread-safe, so every worker opens
    its own session. Do not share the parent `db` session across threads.
    """
    started = time.perf_counter()
    try:
        from app.services.analytics_engine.capabilities.booking_management.rating_runner import (  # noqa: WPS433
            BookingCommunicationReviewRunner,
        )

        with open_db_session(database_url) as worker_db:
            runner = BookingCommunicationReviewRunner(db=worker_db, schema=schema)
            review = _safe_review_one_call(
                runner,
                booking_id=int(booking_id),
                customer_days=int(customer_days),
                run_llm=True,
                use_cache=True,
                model=model,
                timeout_seconds=int(timeout_seconds),
                max_llm_messages=int(max_llm_messages),
                max_llm_text_chars=int(max_llm_text_chars),
                force_refresh=bool(force_refresh),
                include_context=bool(debug),
                include_prompt=bool(debug),
            )
            if not isinstance(review, dict):
                review = {"booking_id": int(booking_id), "status": "error", "error": "review_one returned non-dict payload"}
            review.setdefault("booking_id", int(booking_id))
            review.setdefault("elapsed_ms", round((time.perf_counter() - started) * 1000, 2))
            return review
    except Exception as exc:
        return {
            "booking_id": int(booking_id),
            "status": "error",
            "cached": False,
            "cache_status": "error",
            "error": f"{exc.__class__.__name__}: {exc}",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }


def _rate_booking_ids_parallel(
    *,
    database_url: Optional[str],
    schema: str,
    booking_ids: list[int],
    customer_days: int,
    model: str,
    timeout_seconds: int,
    max_llm_messages: int,
    max_llm_text_chars: int,
    force_refresh: bool,
    workers: int,
    fail_fast: bool,
    debug: bool,
    mode_label: str,
    started: float | None = None,
    extra_payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Rate an explicit booking-id list concurrently using one DB session per worker."""
    started = started or time.perf_counter()
    unique_booking_ids: list[int] = []
    seen: set[int] = set()
    for value in booking_ids or []:
        if value in (None, ""):
            continue
        booking_id = int(value)
        if booking_id in seen:
            continue
        seen.add(booking_id)
        unique_booking_ids.append(booking_id)

    max_workers = max(1, min(int(workers or 1), len(unique_booking_ids) or 1))
    reviews_by_id: dict[int, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []

    if unique_booking_ids:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    _rate_one_booking_worker,
                    database_url=database_url,
                    schema=schema,
                    booking_id=booking_id,
                    customer_days=int(customer_days),
                    model=model,
                    timeout_seconds=int(timeout_seconds),
                    max_llm_messages=int(max_llm_messages),
                    max_llm_text_chars=int(max_llm_text_chars),
                    force_refresh=bool(force_refresh),
                    debug=bool(debug),
                ): booking_id
                for booking_id in unique_booking_ids
            }
            stop_after_errors = False
            for future in as_completed(future_map):
                booking_id = future_map[future]
                try:
                    review = future.result()
                except Exception as exc:  # defensive; worker should already catch
                    review = {
                        "booking_id": int(booking_id),
                        "status": "error",
                        "cached": False,
                        "cache_status": "error",
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
                reviews_by_id[int(booking_id)] = review
                if review.get("status") == "error":
                    errors.append(review)
                    if fail_fast and not stop_after_errors:
                        stop_after_errors = True
                        for pending in future_map:
                            if not pending.done():
                                pending.cancel()

    reviews = [reviews_by_id[booking_id] for booking_id in unique_booking_ids if booking_id in reviews_by_id]
    summary_rows = [_compact_summary_row(review) for review in reviews]
    ok_count = sum(1 for row in reviews if row.get("status") == "ok")
    error_count = sum(1 for row in reviews if row.get("status") == "error")
    cache_hit_count = sum(1 for row in reviews if row.get("cached") is True or row.get("cache_status") == "hit")
    llm_or_store_count = sum(1 for row in reviews if row.get("cache_status") in {"stored", "error_stored"} or row.get("cached") is False)

    payload: dict[str, Any] = {
        "status": "partial_error" if error_count and ok_count else "error" if error_count and not ok_count else "ok",
        "mode": mode_label,
        "dry_run": False,
        "schema": schema,
        "force_refresh": bool(force_refresh),
        "workers": max_workers,
        "customer_days": int(customer_days),
        "requested_count": len(unique_booking_ids),
        "processed_count": len(reviews),
        "ok_count": ok_count,
        "error_count": error_count,
        "cache_hit_count": cache_hit_count,
        "llm_or_store_count": llm_or_store_count,
        "booking_ids": unique_booking_ids,
        "summary_rows": summary_rows,
        "reviews": reviews,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    if extra_payload:
        payload.update(extra_payload)
    if debug:
        payload["errors"] = errors
    return payload


def rate_active_bookings_parallel(
    db: Session,
    schema: str,
    *,
    database_url: Optional[str],
    scan_limit: int,
    limit: int,
    customer_days: int,
    model: str,
    timeout_seconds: int,
    max_llm_messages: int,
    max_llm_text_chars: int,
    candidate_mode: str,
    force_refresh: bool,
    only_rms_prop: bool,
    workers: int,
    fail_fast: bool,
    debug: bool,
) -> dict[str, Any]:
    """Parallel active/missing/all booking rating.

    Candidate discovery remains single-threaded; expensive LLM calls run in
    parallel. This is now the default path because --workers defaults to 5.
    Use --workers 1 to force old sequential behavior.
    """
    started = time.perf_counter()
    candidates = list_active_booking_rating_candidates(
        db,
        schema,
        scan_limit=int(scan_limit),
        limit=int(limit),
        only_rms_prop=bool(only_rms_prop),
        candidate_mode=candidate_mode,
        force_refresh=bool(force_refresh),
    )
    rows = candidates.get("rows") or []
    booking_ids: list[int] = []
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_id = row.get("booking_id") or row.get("raw_booking_id")
        if raw_id in (None, ""):
            continue
        booking_id = int(raw_id)
        if booking_id in seen:
            continue
        seen.add(booking_id)
        booking_ids.append(booking_id)

    extra_payload = {
        "candidate_mode": candidate_mode,
        "scan_limit": int(scan_limit),
        "limit": int(limit),
        "active_booking_count": candidates.get("active_booking_count"),
        "candidate_count": candidates.get("candidate_count"),
        "truncated": candidates.get("truncated"),
    }
    if debug:
        extra_payload["candidate_rows"] = rows

    return _rate_booking_ids_parallel(
        database_url=database_url,
        schema=schema,
        booking_ids=booking_ids,
        customer_days=customer_days,
        model=model,
        timeout_seconds=timeout_seconds,
        max_llm_messages=max_llm_messages,
        max_llm_text_chars=max_llm_text_chars,
        force_refresh=force_refresh,
        workers=workers,
        fail_fast=fail_fast,
        debug=debug,
        mode_label="active_booking_rating_cron_parallel",
        started=started,
        extra_payload=extra_payload,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rate active bookings into booking_communication_review cache.")
    parser.add_argument(
        "--mode",
        choices=["active", "active-all", "missing", "stale", "changed", "dry-run"],
        default="active",
        help=(
            "active = active missing/stale/non-ok only; "
            "active-all = scan all active but ok rows fast-hit unless --force-refresh; "
            "missing = active bookings with no cache only; "
            "stale = stale cache rows; "
            "changed = mark ok rows stale if new comms arrived since last review, then rate; "
            "dry-run = show active candidates only."
        ),
    )
    parser.add_argument("--scan-limit", type=int, default=5000, help="Max active bookings to scan before candidate filtering.")
    parser.add_argument("--scan-batch-size", type=int, default=50, help="Changed mode page size. Scan recent activity IDs in small pages until --limit candidates are found.")
    parser.add_argument("--limit", type=int, default=25, help="Max bookings to rate in this run. Keep low for frequent cron.")
    parser.add_argument("--customer-days", type=int, default=30, help="Customer brief conversation lookback days.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--max-llm-messages", type=int, default=12)
    parser.add_argument("--max-llm-text-chars", type=int, default=220)
    parser.add_argument("--sleep-seconds", type=float, default=0.0, help="Optional pause between LLM calls when --workers 1.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Parallel LLM workers. Default is 5 via RATING_REVIEW_WORKERS/BOOKING_RATING_WORKERS. Use --workers 1 for sequential.")
    parser.add_argument("--force-refresh", action="store_true", help="Recompute even when cache is already ok. Use carefully.")
    parser.add_argument("--include-non-rms", action="store_true", help="Include non-RMS Prop active bookings. Default is RMS Prop only.")
    parser.add_argument("--dry-run", action="store_true", help="Show candidates without calling LLM.")
    parser.add_argument("--activity-days", type=int, default=2, help="Days to look back for new communication activity in --mode changed.")
    parser.add_argument("--include-contact-scans", action="store_true", help="Scan calls/WhatsApp/email in --mode changed. Required to detect all channels.")
    parser.add_argument("--fail-fast", action="store_true", help="Stop at first booking error.")
    parser.add_argument("--debug", action="store_true", help="Include full per-booking payloads in stdout.")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL") or os.getenv("PG_URL"))
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--output", default="", help="Optional JSON output path.")
    parser.add_argument("--booking-id", type=int, help="Single booking ID to rate (overrides normal candidate discovery)")
    parser.add_argument("--min-review-age-minutes", type=int, default=300, help="Skip booking if last review is younger than this many minutes")
    return parser


def main() -> int:
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    args = build_parser().parse_args()
    if args.booking_id:
        with open_db_session(args.database_url) as db:
            payload = check_and_rate_single_booking(
                db,
                args.schema,
                booking_id=args.booking_id,
                customer_days=args.customer_days,
                model=args.model,
                timeout_seconds=args.timeout_seconds,
                max_llm_messages=args.max_llm_messages,
                max_llm_text_chars=args.max_llm_text_chars,
                dry_run=bool(args.dry_run or args.mode == "dry-run"),
                debug=args.debug,
            )
        output = json_dumps(payload, pretty=args.pretty)
        if args.output:
            Path(args.output).write_text(output, encoding="utf-8")
        print(output)
        return 0 if payload.get("status") in {"ok", "no_new_communication"} else 1
    try:
        with open_db_session(args.database_url) as db:
            dry_run = bool(args.dry_run or args.mode == "dry-run")
            if args.mode == "stale" and not dry_run:
                if int(args.workers or 1) > 1:
                    booking_ids = list_stale_booking_ids(db, args.schema, limit=int(args.limit))
                    payload = _rate_booking_ids_parallel(
                        database_url=args.database_url,
                        schema=args.schema,
                        booking_ids=booking_ids,
                        customer_days=args.customer_days,
                        model=args.model,
                        timeout_seconds=args.timeout_seconds,
                        max_llm_messages=args.max_llm_messages,
                        max_llm_text_chars=args.max_llm_text_chars,
                        force_refresh=True,
                        workers=args.workers,
                        fail_fast=args.fail_fast,
                        debug=args.debug,
                        mode_label="stale_booking_rating_cron_parallel",
                        extra_payload={"selection_mode": "stale", "limit": int(args.limit)},
                    )
                else:
                    payload = recompute_stale_bookings(
                        db,
                        args.schema,
                        limit=args.limit,
                        customer_days=args.customer_days,
                        model=args.model,
                        timeout_seconds=args.timeout_seconds,
                    )
            elif args.mode == "changed":
                payload = rate_bookings_with_new_communication(
                    db,
                    args.schema,
                    limit=args.limit,
                    scan_limit=args.scan_limit,
                    scan_batch_size=args.scan_batch_size,
                    activity_days=args.activity_days,
                    include_contact_scans=args.include_contact_scans,
                    min_review_age_minutes=args.min_review_age_minutes,
                    customer_days=args.customer_days,
                    model=args.model,
                    timeout_seconds=args.timeout_seconds,
                    max_llm_messages=args.max_llm_messages,
                    max_llm_text_chars=args.max_llm_text_chars,
                    dry_run=dry_run,
                    fail_fast=args.fail_fast,
                    debug=args.debug,
                )
            elif dry_run:
                mode = "all" if args.mode == "active-all" else "missing" if args.mode == "missing" else "due"
                payload = list_active_booking_rating_candidates(
                    db,
                    args.schema,
                    scan_limit=args.scan_limit,
                    limit=args.limit,
                    only_rms_prop=not args.include_non_rms,
                    candidate_mode=mode,
                    force_refresh=args.force_refresh,
                )
                payload = {"status": "ok", "dry_run": True, "job": "booking_rating_reviews", **payload}
            else:
                if args.mode == "active-all":
                    candidate_mode = "all"
                elif args.mode == "missing":
                    candidate_mode = "missing"
                else:
                    candidate_mode = "due"
                if int(args.workers or 1) > 1:
                    payload = rate_active_bookings_parallel(
                        db,
                        args.schema,
                        database_url=args.database_url,
                        scan_limit=args.scan_limit,
                        limit=args.limit,
                        customer_days=args.customer_days,
                        model=args.model,
                        timeout_seconds=args.timeout_seconds,
                        max_llm_messages=args.max_llm_messages,
                        max_llm_text_chars=args.max_llm_text_chars,
                        candidate_mode=candidate_mode,
                        force_refresh=args.force_refresh,
                        only_rms_prop=not args.include_non_rms,
                        workers=args.workers,
                        fail_fast=args.fail_fast,
                        debug=args.debug,
                    )
                else:
                    payload = rate_active_bookings(
                        db,
                        args.schema,
                        scan_limit=args.scan_limit,
                        limit=args.limit,
                        customer_days=args.customer_days,
                        model=args.model,
                        timeout_seconds=args.timeout_seconds,
                        max_llm_messages=args.max_llm_messages,
                        max_llm_text_chars=args.max_llm_text_chars,
                        candidate_mode=candidate_mode,
                        force_refresh=args.force_refresh,
                        only_rms_prop=not args.include_non_rms,
                        dry_run=False,
                        sleep_seconds=args.sleep_seconds,
                        fail_fast=args.fail_fast,
                        debug=args.debug,
                    )
    except Exception as exc:
        payload = {
            "status": "error",
            "error_type": exc.__class__.__name__,
            "error": str(exc),
            "job": "booking_rating_reviews",
        }
        output = json_dumps(payload, pretty=args.pretty)
        if args.output:
            Path(args.output).write_text(output, encoding="utf-8")
        print(output, file=sys.stderr)
        return 1

    payload = _clean_payload_for_stdout(payload, debug=args.debug)
    output = json_dumps(payload, pretty=args.pretty)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    print(output)
    return 0 if payload.get("status") in {"ok", "partial_error"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
