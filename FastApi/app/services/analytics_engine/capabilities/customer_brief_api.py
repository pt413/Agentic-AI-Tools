#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


# -----------------------------------------------------------------------------
# Repo / env bootstrap
# -----------------------------------------------------------------------------

def _project_root() -> Path:
    current = Path(__file__).resolve().parent
    root = current
    for _ in range(8):
        if (root / "app").exists():
            return root
        root = root.parent
    return current


PROJECT_ROOT = _project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _try_load_env() -> None:
    if load_dotenv is None:
        return
    for path in (PROJECT_ROOT / ".env", PROJECT_ROOT.parent / ".env", Path.cwd() / ".env"):
        if path.exists():
            load_dotenv(path, override=False)


def _get_db_url(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    db_url = os.getenv("DATABASE_URL") or os.getenv("PG_URL")
    if db_url:
        return db_url
    _try_load_env()
    db_url = os.getenv("DATABASE_URL") or os.getenv("PG_URL")
    if db_url:
        return db_url
    try:
        from app.database.session import DATABASE_URL as SESSION_DATABASE_URL  # type: ignore

        if SESSION_DATABASE_URL:
            return SESSION_DATABASE_URL
    except Exception:
        pass
    return None


def _open_db_session(database_url: str | None):
    """
    Repo-aligned DB access:
    1) try app.db.database.SessionLocal
    2) fallback to create_engine/sessionmaker using DATABASE_URL/PG_URL
    Returns: (engine_or_none, session_or_none, status)
    """
    try:
        from app.db.database import SessionLocal  # type: ignore

        db = SessionLocal()
        return None, db, "ready_repo_sessionlocal"
    except Exception:
        pass

    if not database_url:
        return None, None, "missing_database_url"

    try:
        engine = create_engine(database_url, pool_pre_ping=True)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        db = SessionLocal()
        return engine, db, "ready_fallback_engine"
    except Exception as exc:
        return None, None, f"db_init_failed: {exc}"


# -----------------------------------------------------------------------------
# Main brief builder
# -----------------------------------------------------------------------------

def build_customer_brief(
    *,
    database_url: str | None,
    schema: str,
    booking_id: int,
    days: int,
    verbose: bool,
    output_format: str = "llm",
    max_llm_messages: int = 6,
    max_llm_text_chars: int = 120,
) -> dict:
    if booking_id in (None, ""):
        raise ValueError("Provide --booking-id.")

    started_total = time.perf_counter()
    db_url = _get_db_url(database_url)
    engine, db, db_status = _open_db_session(db_url)
    if db is None:
        raise RuntimeError(f"Could not open DB session: {db_status}")

    try:
        from app.services.analytics_engine.capabilities.customer_brief_service import CustomerBriefService

        service = CustomerBriefService(db=db, schema=schema)
        payload = service.build(booking_id=int(booking_id), conversation_days=days)

        normalized_output_format = str(output_format or "llm").strip().lower()
        if normalized_output_format == "llm":
            return service.compact_for_llm(
                payload,
                max_messages=max_llm_messages,
                max_text_chars=max_llm_text_chars,
            )

        if normalized_output_format == "both":
            payload["llm_context"] = service.compact_for_llm(
                payload,
                max_messages=max_llm_messages,
                max_text_chars=max_llm_text_chars,
            )

        if verbose:
            payload["debug"] = {
                "timing_total_seconds": round(time.perf_counter() - started_total, 2),
                "builder": "CustomerBriefService",
                "database_status": db_status,
                "schema": schema,
                "booking_id": int(booking_id),
                "conversation_days": days,
                "scope": "booking_id_only",
            }

        return payload
    finally:
        try:
            db.close()
        except Exception:
            pass
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch booking-scoped customer brief. Only --booking-id is supported."
    )
    parser.add_argument("--booking-id", type=int, required=True, help="Booking ID")
    parser.add_argument("--days", type=int, default=30, help="Conversation lookback days")
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL") or os.getenv("PG_URL"),
        help="Postgres connection URL (defaults to DATABASE_URL/PG_URL)",
    )
    parser.add_argument("--schema", default="AnalyticsEngine", help="Analytics schema name")
    parser.add_argument("--pretty", action="store_true", help="Pretty JSON output")
    parser.add_argument("--verbose", action="store_true", help="Include timing/debug info in full/both output")
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=["llm", "full", "both"],
        default="llm",
        help="Output shape. Default is llm compact. Use full only for debugging raw details.",
    )
    parser.add_argument("--llm", action="store_true", help="Shortcut for --format llm")
    parser.add_argument("--max-llm-messages", type=int, default=6, help="Recent conversation messages to keep in LLM output")
    parser.add_argument("--max-llm-text-chars", type=int, default=120, help="Max chars per message text in LLM output")
    args = parser.parse_args()

    if args.llm:
        args.output_format = "llm"

    try:
        payload = build_customer_brief(
            database_url=args.database_url,
            schema=args.schema,
            booking_id=args.booking_id,
            days=args.days,
            verbose=args.verbose,
            output_format=args.output_format,
            max_llm_messages=args.max_llm_messages,
            max_llm_text_chars=args.max_llm_text_chars,
        )
        print(json.dumps(payload, indent=2 if args.pretty else None, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
