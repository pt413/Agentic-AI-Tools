#!/usr/bin/env python3
"""
run_lead_rating_reviews.py

Batch-create/recompute lead communication ratings for active leads.

Recommended location:
  app/services/analytics_engine/jobs/regular/run_lead_rating_reviews.py

Examples:
  # Rate active leads that do not already have a good cache row
  python -m app.services.analytics_engine.jobs.regular.run_lead_rating_reviews --mode active --limit 100 --days 90

  # Rate active leads in parallel, like booking_rating_reviews
  python -m app.services.analytics_engine.jobs.regular.run_lead_rating_reviews --mode active --limit 100 --workers 5 --days 90

  # Recompute stale/non-ok cache rows only
  python -m app.services.analytics_engine.jobs.regular.run_lead_rating_reviews --mode stale --limit 50 --workers 5

  # Force refresh a few leads
  python -m app.services.analytics_engine.jobs.regular.run_lead_rating_reviews --lead-ids 401676,398048 --force-refresh --workers 2

  # Dry run selected active leads
  python -m app.services.analytics_engine.jobs.regular.run_lead_rating_reviews --mode active --limit 20 --dry-run
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


def _project_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent, *current.parents]:
        if (parent / "app").is_dir():
            return parent
    return Path.cwd()


PROJECT_ROOT = _project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.analytics_engine.capabilities.lead_management.cache import (  # noqa: E402
    LEAD_LLM_REVIEW_CACHE_TABLE,
    ensure_lead_review_cache_table,
    schema_ident,
)
from app.services.analytics_engine.capabilities.lead_management.jobs import list_stale_lead_ids  # noqa: E402
from app.services.analytics_engine.capabilities.lead_management.rating_runner import LeadCommunicationReviewRunner  # noqa: E402


DEFAULT_SCHEMA = os.getenv("SCHEMA_NAME", "AnalyticsEngine")
DEFAULT_CLOSED_STATUSES = (
    "booked",
    "converted",
    "closed",
    "cancelled",
    "canceled",
    "lost",
    "not interested",
    "not_interested",
    "junk",
    "duplicate",
)


def _try_load_env() -> None:
    if load_dotenv is None:
        return
    for env_path in (
        PROJECT_ROOT / ".env",
        PROJECT_ROOT.parent / ".env",
        Path.cwd() / ".env",
        Path.cwd().parent / ".env",
    ):
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
    """Open a DB session for the current process/thread.

    SQLAlchemy sessions are not thread-safe, so every parallel worker must open
    its own session instead of sharing the parent selection session.
    """
    if not database_url:
        try:
            from app.db.database import SessionLocal  # type: ignore  # noqa: WPS433

            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()
            return
        except Exception:
            pass

        # Compatibility fallback for repo versions that expose get_db() but not
        # SessionLocal directly.
        repo_gen = None
        repo_db = None
        try:
            from app.db.database import get_db  # type: ignore  # noqa: WPS433

            repo_gen = get_db()
            repo_db = next(repo_gen)
            try:
                yield repo_db
            finally:
                try:
                    repo_db.close()
                except Exception:
                    pass
                try:
                    next(repo_gen, None)
                except Exception:
                    try:
                        repo_gen.close()
                    except Exception:
                        pass
            return
        except Exception:
            if repo_db is not None:
                try:
                    repo_db.close()
                except Exception:
                    pass
            if repo_gen is not None:
                try:
                    repo_gen.close()
                except Exception:
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


def _parse_csv_ints(value: str | None) -> list[int]:
    out: list[int] = []
    for part in str(value or "").split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return list(dict.fromkeys(out))


def _parse_csv_text(value: str | None) -> list[str]:
    return [part.strip().lower() for part in str(value or "").split(",") if part.strip()]


def _lead_table_columns(db: Session, schema: str) -> set[str]:
    rows = db.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = :schema_name
              AND table_name = 'staging_lead_tracking'
            """
        ),
        {"schema_name": schema},
    ).mappings().fetchall()
    return {str(row["column_name"]) for row in rows if row.get("column_name")}


def _named_in(values: Iterable[Any], prefix: str) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {}
    holders: list[str] = []
    for idx, value in enumerate(values):
        key = f"{prefix}_{idx}"
        holders.append(f":{key}")
        params[key] = value
    return ", ".join(holders) or "NULL", params


def select_active_lead_ids(
    db: Session,
    *,
    schema: str,
    limit: int,
    offset: int = 0,
    recent_days: int | None = None,
    include_statuses: list[str] | None = None,
    exclude_statuses: list[str] | None = None,
    only_missing_or_stale: bool = True,
) -> list[int]:
    """Select active lead IDs to rate.

    Default active definition:
      raw_status/status not in common closed statuses.

    If your CRM uses different status names, pass --include-statuses or
    --exclude-statuses explicitly.
    """
    ensure_lead_review_cache_table(db, schema)
    columns = _lead_table_columns(db, schema)
    if "source_id" not in columns:
        raise RuntimeError("staging_lead_tracking.source_id is required")

    status_expr = "NULL"
    for candidate in ("raw_status", "lead_status", "status"):
        if candidate in columns:
            status_expr = f"LOWER(TRIM(COALESCE(l.{candidate}::text, '')))"
            break

    time_expr = "NULL"
    for candidate in ("created_at", "updated_at", "synced_at", "closed_at"):
        if candidate in columns:
            time_expr = f"l.{candidate}"
            break

    lead_table = f"{schema_ident(schema)}.staging_lead_tracking"
    rating_table = f"{schema_ident(schema)}.{LEAD_LLM_REVIEW_CACHE_TABLE}"

    where_parts = ["l.source_id IS NOT NULL"]
    params: dict[str, Any] = {
    "limit_n": int(limit),
    "offset_n": int(offset or 0),
}

    if include_statuses:
        in_sql, in_params = _named_in([item.lower() for item in include_statuses], "include_status")
        params.update(in_params)
        where_parts.append(f"{status_expr} IN ({in_sql})")
    else:
        excluded = exclude_statuses or list(DEFAULT_CLOSED_STATUSES)
        in_sql, in_params = _named_in([item.lower() for item in excluded], "exclude_status")
        params.update(in_params)
        where_parts.append(f"({status_expr} IS NULL OR {status_expr} = '' OR {status_expr} NOT IN ({in_sql}))")

    if recent_days and time_expr != "NULL":
        params["recent_days"] = int(recent_days)
        where_parts.append(f"{time_expr} >= NOW() - (:recent_days * INTERVAL '1 day')")

    if only_missing_or_stale:
        where_parts.append(
            "(r.lead_id IS NULL OR LOWER(COALESCE(r.status::text, '')) <> 'ok' OR r.stale_at IS NOT NULL)"
        )

    order_expr = time_expr if time_expr != "NULL" else "l.source_id"
    rows = db.execute(
        text(
            f"""
            SELECT l.source_id::bigint AS lead_id
            FROM {lead_table} l
            LEFT JOIN {rating_table} r
              ON r.lead_id = l.source_id
            WHERE {' AND '.join(where_parts)}
            ORDER BY {order_expr} DESC NULLS LAST, l.source_id DESC
            LIMIT :limit_n
            OFFSET :offset_n
            """
        ),
        params,
    ).mappings().fetchall()
    return [int(row["lead_id"]) for row in rows if row.get("lead_id") is not None]


def _safe_review_one_call(runner: Any, **kwargs: Any) -> dict[str, Any]:
    """Call review_one while tolerating small signature differences across repo versions."""
    sig = inspect.signature(runner.review_one)
    accepted = {key: value for key, value in kwargs.items() if key in sig.parameters}
    return runner.review_one(**accepted)


def _compact_result_from_review(
    *,
    lead_id: int,
    review: dict[str, Any],
    elapsed_sec: float,
    debug: bool = False,
) -> dict[str, Any]:
    item = {
        "lead_id": int(lead_id),
        "status": review.get("status"),
        "cached": review.get("cached"),
        "cache_status": review.get("cache_status"),
        "overall_score": review.get("overall_score") or review.get("score"),
        "overall_priority_score": review.get("overall_priority_score") or review.get("priority_score"),
        "lead_handling_score": review.get("lead_handling_score"),
        "customer_perspective_score": review.get("customer_perspective_score"),
        "overall_risk": review.get("overall_risk") or review.get("risk"),
        "main_reason": review.get("main_reason"),
        "error": review.get("error"),
        "elapsed_sec": round(float(elapsed_sec), 3),
    }
    compact = {key: value for key, value in item.items() if value not in (None, "", [], {})}
    if debug:
        # Keep debug explicit; normal cron logs should stay compact.
        compact["review"] = review
    return compact


def _rate_one_lead_worker(
    *,
    database_url: Optional[str],
    schema: str,
    lead_id: int,
    days: int,
    model: str,
    timeout_seconds: int,
    force_refresh: bool,
    debug: bool,
) -> dict[str, Any]:
    """Rate exactly one lead in its own DB session."""
    started = time.perf_counter()
    try:
        with open_db_session(database_url) as worker_db:
            runner = LeadCommunicationReviewRunner(db=worker_db, schema=schema)
            review = _safe_review_one_call(
                runner,
                lead_id=int(lead_id),
                days=int(days),
                run_llm=True,
                model=model,
                timeout_seconds=int(timeout_seconds),
                use_cache=True,
                force_refresh=bool(force_refresh),
            )
            if not isinstance(review, dict):
                review = {"lead_id": int(lead_id), "status": "error", "error": "review_one returned non-dict payload"}
            return _compact_result_from_review(
                lead_id=int(lead_id),
                review=review,
                elapsed_sec=time.perf_counter() - started,
                debug=debug,
            )
    except Exception as exc:
        return {
            "lead_id": int(lead_id),
            "status": "error",
            "cached": False,
            "cache_status": "error",
            "error": f"{exc.__class__.__name__}: {exc}",
            "elapsed_sec": round(time.perf_counter() - started, 3),
        }


def _aggregate_payload(
    *,
    selection_mode: str,
    lead_ids: list[int],
    results: list[dict[str, Any]],
    started: float,
    workers: int,
    parallel: bool,
    force_refresh: bool,
    days: int,
    note: str | None = None,
) -> dict[str, Any]:
    ok_count = sum(1 for item in results if item.get("status") == "ok")
    error_count = sum(1 for item in results if item.get("status") == "error")
    cache_hit_count = sum(1 for item in results if item.get("cached") is True or item.get("cache_status") == "hit")
    llm_or_store_count = sum(
        1
        for item in results
        if item.get("cache_status") in {"stored", "error_stored"} or item.get("cached") is False
    )
    payload = {
        "status": "partial_error" if error_count and ok_count else "error" if error_count and not ok_count else "ok",
        "job": "lead_rating_reviews",
        "mode": selection_mode,
        "parallel": bool(parallel),
        "workers": int(workers),
        "force_refresh": bool(force_refresh),
        "days": int(days),
        "requested": len(lead_ids),
        "processed": len(results),
        "ok": ok_count,
        "errors": error_count,
        "cached": cache_hit_count,
        "llm_or_store_count": llm_or_store_count,
        "elapsed_sec": round(time.perf_counter() - started, 3),
        "lead_ids": lead_ids,
        "results": results,
    }
    if note:
        payload["note"] = note
    return payload


def rate_leads_parallel(
    *,
    database_url: Optional[str],
    schema: str,
    lead_ids: list[int],
    selection_mode: str,
    days: int,
    model: str,
    timeout_seconds: int,
    force_refresh: bool,
    workers: int,
    fail_fast: bool,
    debug: bool,
    sleep_seconds: float = 0.0,
) -> dict[str, Any]:
    """Parallel lead rating, modeled after booking_rating_reviews.

    Candidate selection stays single-threaded. Each expensive LLM review runs in
    a separate worker with a separate DB session.
    """
    started = time.perf_counter()
    max_workers = max(1, min(int(workers or 1), len(lead_ids) or 1))
    results_by_id: dict[int, dict[str, Any]] = {}
    completed = 0
    note = "sleep_seconds is ignored when workers > 1" if sleep_seconds else None

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _rate_one_lead_worker,
                database_url=database_url,
                schema=schema,
                lead_id=int(lead_id),
                days=int(days),
                model=model,
                timeout_seconds=int(timeout_seconds),
                force_refresh=bool(force_refresh),
                debug=bool(debug),
            ): int(lead_id)
            for lead_id in lead_ids
        }
        for future in as_completed(future_map):
            lead_id = future_map[future]
            try:
                item = future.result()
            except Exception as exc:  # defensive; worker should already catch
                item = {
                    "lead_id": int(lead_id),
                    "status": "error",
                    "cached": False,
                    "cache_status": "error",
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            results_by_id[int(lead_id)] = item
            completed += 1
            print(json.dumps({"progress": f"{completed}/{len(lead_ids)}", **item}, ensure_ascii=False, default=str), flush=True)
            if fail_fast and item.get("status") == "error":
                # Already-submitted futures cannot be safely killed. We still
                # collect them so DB sessions close cleanly, matching the booking
                # script's conservative behavior.
                pass

    results = [results_by_id[lead_id] for lead_id in lead_ids if lead_id in results_by_id]
    return _aggregate_payload(
        selection_mode=selection_mode,
        lead_ids=lead_ids,
        results=results,
        started=started,
        workers=max_workers,
        parallel=True,
        force_refresh=force_refresh,
        days=days,
        note=note,
    )


def rate_leads_sequential(
    *,
    db: Session,
    schema: str,
    lead_ids: list[int],
    selection_mode: str,
    days: int,
    model: str,
    timeout_seconds: int,
    force_refresh: bool,
    sleep_seconds: float,
    debug: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    runner = LeadCommunicationReviewRunner(db=db, schema=schema)
    results: list[dict[str, Any]] = []

    for idx, lead_id in enumerate(lead_ids, start=1):
        item_started = time.perf_counter()
        try:
            review = _safe_review_one_call(
                runner,
                lead_id=int(lead_id),
                days=int(days),
                run_llm=True,
                model=model,
                timeout_seconds=int(timeout_seconds),
                use_cache=True,
                force_refresh=bool(force_refresh),
            )
            if not isinstance(review, dict):
                review = {"lead_id": int(lead_id), "status": "error", "error": "review_one returned non-dict payload"}
            item = _compact_result_from_review(
                lead_id=int(lead_id),
                review=review,
                elapsed_sec=time.perf_counter() - item_started,
                debug=debug,
            )
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            item = {
                "lead_id": int(lead_id),
                "status": "error",
                "error": f"{exc.__class__.__name__}: {exc}",
                "elapsed_sec": round(time.perf_counter() - item_started, 3),
            }
        results.append(item)
        print(json.dumps({"progress": f"{idx}/{len(lead_ids)}", **item}, ensure_ascii=False, default=str), flush=True)
        if sleep_seconds and idx < len(lead_ids):
            time.sleep(float(sleep_seconds))

    return _aggregate_payload(
        selection_mode=selection_mode,
        lead_ids=lead_ids,
        results=results,
        started=started,
        workers=1,
        parallel=False,
        force_refresh=force_refresh,
        days=days,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create/recompute LLM ratings for active leads.")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--mode", choices=["active", "stale"], default="active")
    parser.add_argument("--lead-ids", default=None, help="Comma-separated explicit lead IDs. Overrides --mode selection.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0, help="Skip this many selected leads before processing.")
    parser.add_argument("--days", type=int, default=90, help="Evidence window for each lead review.")
    parser.add_argument("--recent-days", type=int, default=180, help="Only pick active leads created/updated in this many days. Use 0 to disable.")
    parser.add_argument("--include-statuses", default=None, help="Comma-separated statuses to include, e.g. Waiting,SiteVisit,Followup.")
    parser.add_argument("--exclude-statuses", default=None, help="Comma-separated statuses to exclude. Defaults to common closed/lost/booked statuses.")
    parser.add_argument("--only-missing-or-stale", default="true", help="true/false. Skip already ok cache rows in active mode.")
    parser.add_argument("--force-refresh", action="store_true", help="Recompute even if cache status is ok.")
    parser.add_argument("--model", default="builtin")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--sleep-seconds", type=float, default=0.0, help="Delay between LLM calls. Ignored when --workers > 1.")
    parser.add_argument("--workers", type=int, default=1, help="Parallel LLM workers. Start with 3-5.")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL") or os.getenv("PG_URL"))
    parser.add_argument("--fail-fast", action="store_true", help="Keep for CLI compatibility; already-running futures are still collected.")
    parser.add_argument("--debug", action="store_true", help="Include full per-lead review payload in result rows.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--output", default="", help="Optional JSON output path.")
    return parser


def _select_leads(db: Session, args: argparse.Namespace) -> tuple[list[int], str]:
    include_statuses = _parse_csv_text(args.include_statuses)
    exclude_statuses = _parse_csv_text(args.exclude_statuses)
    explicit_lead_ids = _parse_csv_ints(args.lead_ids)
    only_missing_or_stale = str(args.only_missing_or_stale).strip().lower() not in {"0", "false", "no", "n"}

    ensure_lead_review_cache_table(db, args.schema)

    if explicit_lead_ids:
        return explicit_lead_ids[: int(args.limit)], "explicit"
    if args.mode == "stale":
        return list_stale_lead_ids(db, args.schema, limit=int(args.limit)), "stale"
    return (
        select_active_lead_ids(
            db,
            schema=args.schema,
            limit=int(args.limit),
            offset=int(args.offset or 0),
            recent_days=None if int(args.recent_days or 0) <= 0 else int(args.recent_days),
            include_statuses=include_statuses or None,
            exclude_statuses=exclude_statuses or None,
            only_missing_or_stale=only_missing_or_stale and not bool(args.force_refresh),
        ),
        f"active_offset_{int(args.offset or 0)}",
    )


def main() -> int:
    args = build_parser().parse_args()
    try:
        with open_db_session(args.database_url) as db:
            lead_ids, selection_mode = _select_leads(db, args)
            dry_run = bool(args.dry_run)
            if dry_run:
                payload = {
                    "status": "ok",
                    "job": "lead_rating_reviews",
                    "mode": selection_mode,
                    "dry_run": True,
                    "count": len(lead_ids),
                    "lead_ids": lead_ids,
                }
            else:
                force_refresh = bool(args.force_refresh or selection_mode == "stale")
                if int(args.workers or 1) > 1:
                    payload = rate_leads_parallel(
                        database_url=args.database_url,
                        schema=args.schema,
                        lead_ids=lead_ids,
                        selection_mode=selection_mode,
                        days=args.days,
                        model=args.model,
                        timeout_seconds=args.timeout_seconds,
                        force_refresh=force_refresh,
                        workers=args.workers,
                        fail_fast=args.fail_fast,
                        debug=args.debug,
                        sleep_seconds=args.sleep_seconds,
                    )
                else:
                    payload = rate_leads_sequential(
                        db=db,
                        schema=args.schema,
                        lead_ids=lead_ids,
                        selection_mode=selection_mode,
                        days=args.days,
                        model=args.model,
                        timeout_seconds=args.timeout_seconds,
                        force_refresh=force_refresh,
                        sleep_seconds=args.sleep_seconds,
                        debug=args.debug,
                    )
    except Exception as exc:
        payload = {
            "status": "error",
            "error_type": exc.__class__.__name__,
            "error": str(exc),
            "job": "lead_rating_reviews",
        }
        output = json.dumps(payload, indent=2 if getattr(args, "pretty", False) else None, ensure_ascii=False, default=str)
        if getattr(args, "output", ""):
            Path(args.output).write_text(output, encoding="utf-8")
        print(output, file=sys.stderr)
        return 1

    output = json.dumps(payload, indent=2 if args.pretty else None, ensure_ascii=False, default=str)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    print(output)
    return 0 if payload.get("status") in {"ok", "partial_error"} else 1


if __name__ == "__main__":
    raise SystemExit(main())