#!/usr/bin/env python3
"""Compatibility wrapper for booking communication review / dashboard.

Implementation is split under booking_management/:
- common.py
- prompt.py
- parsing.py
- llm_client.py
- cache.py
- rating_runner.py
- dashboard.py
- jobs.py

Use this module from routes and scripts. It provides the same cache-first shape
as lead_management, but for booking_id.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

from app.services.analytics_engine.capabilities.booking_management.common import *  # noqa: F401,F403
from app.services.analytics_engine.capabilities.booking_management.prompt import *  # noqa: F401,F403
from app.services.analytics_engine.capabilities.booking_management.parsing import *  # noqa: F401,F403
from app.services.analytics_engine.capabilities.booking_management.llm_client import *  # noqa: F401,F403
from app.services.analytics_engine.capabilities.booking_management.cache import *  # noqa: F401,F403
from app.services.analytics_engine.capabilities.booking_management.dashboard import list_booking_dashboard_rows
from app.services.analytics_engine.capabilities.booking_management.jobs import (
    list_active_booking_ids,
    list_active_booking_rating_candidates,
    list_active_booking_rows,
    list_stale_booking_ids,
    mark_bookings_stale,
    rate_active_bookings,
    recompute_stale_bookings,
)
from app.services.analytics_engine.capabilities.booking_management.rating_runner import BookingCommunicationReviewRunner


def _project_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent, *current.parents]:
        if (parent / "app").is_dir():
            return parent
    return Path.cwd()


PROJECT_ROOT = _project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _try_load_env() -> None:
    if load_dotenv is None:
        return
    for env_path in (PROJECT_ROOT / ".env", PROJECT_ROOT.parent / ".env", Path.cwd() / ".env", Path.cwd().parent / ".env"):
        if env_path.exists():
            load_dotenv(env_path, override=False)


@contextmanager
def open_db_session(database_url: Optional[str] = None) -> Iterable[Session]:
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
            pass
    _try_load_env()
    db_url = database_url or os.getenv("DATABASE_URL") or os.getenv("PG_URL")
    if not db_url:
        raise RuntimeError("No DB available. Run inside repo or set DATABASE_URL / PG_URL, or pass --database-url.")
    engine = create_engine(db_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        try:
            db.close()
        finally:
            engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Cache-first booking communication review for one booking_id.")
    parser.add_argument("--booking-id", type=int, required=True)
    parser.add_argument("--customer-days", type=int, default=30)
    parser.add_argument("--run-llm", action="store_true", help="Call LLM when cache is missing/stale/non-ok.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--max-llm-messages", type=int, default=12)
    parser.add_argument("--max-llm-text-chars", type=int, default=220)
    parser.add_argument("--use-cache", default="true")
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--include-context", action="store_true")
    parser.add_argument("--include-prompt", action="store_true")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL") or os.getenv("PG_URL"))
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    try:
        with open_db_session(args.database_url) as db:
            runner = BookingCommunicationReviewRunner(db=db, schema=args.schema)
            payload = runner.review_one(
                booking_id=args.booking_id,
                customer_days=args.customer_days,
                run_llm=args.run_llm,
                model=args.model,
                timeout_seconds=args.timeout_seconds,
                max_llm_messages=args.max_llm_messages,
                max_llm_text_chars=args.max_llm_text_chars,
                use_cache=parse_bool(args.use_cache, default=True),
                force_refresh=args.force_refresh,
                include_context=args.include_context,
                include_prompt=args.include_prompt,
            )
        print(json.dumps(payload, ensure_ascii=False, default=str, indent=2 if args.pretty else None))
        return 0
    except Exception as exc:
        print(json.dumps({"error": f"{exc.__class__.__name__}: {exc}"}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
