from __future__ import annotations

# -----------------------------------------------------------------------------
# Repo bootstrap
# -----------------------------------------------------------------------------
import os
import sys
from pathlib import Path

def _analytics_engine_project_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent, *current.parents]:
        if (parent / "app").is_dir():
            return parent
    raise RuntimeError("Could not find FastApi project root containing the app/ folder.")

PROJECT_ROOT = _analytics_engine_project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))



# run_daily_stage_sync_all.py

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


from app.services.analytics_engine.ingestion.staging_sync_service import (  # noqa: E402
    AnalyticsStagingSyncService,
)


DAILY_SYNC_TABLES = [
    # CRM / identity / lead
    {"sync_name": "user_account", "mode": "time", "limit": 50000},
    {"sync_name": "admin_user_account", "mode": "daily", "limit": 50000},
    {"sync_name": "lead_tracking", "mode": "time", "limit": 50000},
    {"sync_name": "building_details", "mode": "hybrid", "limit": 50000},

    # communication
    {"sync_name": "call_log_tracking", "mode": "time", "limit": 50000},
    {"sync_name": "whatsapp_messages", "mode": "time", "limit": 50000},
    {"sync_name": "email_messages", "mode": "time", "limit": 30000},

    # lifecycle / booking
    {"sync_name": "booking_confirm", "mode": "time", "limit": 50000},
    {"sync_name": "booking_audit_history", "mode": "time", "limit": 50000},
    {"sync_name": "booking_invoice_details", "mode": "time", "limit": 50000},

    # customer activity
    {"sync_name": "site_visits", "mode": "time", "limit": 50000},
    {"sync_name": "travel_cart", "mode": "time", "limit": 50000},
    {"sync_name": "user_wishlist", "mode": "time", "limit": 50000},
    {"sync_name": "web_visits", "mode": "id", "limit": 50000},

    # stay/support
    {"sync_name": "checkin_form", "mode": "time", "limit": 30000},
    {"sync_name": "checkout_form", "mode": "time", "limit": 30000},
    {"sync_name": "user_ticket", "mode": "time", "limit": 50000},
]


def _try_load_env() -> None:
    if load_dotenv is None:
        return

    candidates = [
        PROJECT_ROOT / ".env",
        PROJECT_ROOT.parent / ".env",
        Path.cwd() / ".env",
    ]

    for env_path in candidates:
        if env_path.exists():
            load_dotenv(env_path, override=False)


def get_analytics_db_url() -> str:
    db_url = os.getenv("DATABASE_URL") or os.getenv("PG_URL")
    if db_url:
        return db_url

    _try_load_env()

    db_url = os.getenv("DATABASE_URL") or os.getenv("PG_URL")
    if db_url:
        return db_url

    try:
        from app.database.session import DATABASE_URL as SESSION_DATABASE_URL

        if SESSION_DATABASE_URL:
            return SESSION_DATABASE_URL
    except Exception:
        pass

    raise RuntimeError(
        "No analytics DB URL found. Set DATABASE_URL or PG_URL in .env/environment."
    )


def run_one_table(
    service: AnalyticsStagingSyncService,
    *,
    sync_name: str,
    mode: str,
    limit: int,
    max_batches: int,
) -> dict:
    table_started = time.perf_counter()
    total_inserted_or_updated = 0
    batches = []
    status = "success"

    for batch_no in range(1, max_batches + 1):
        batch_started = time.perf_counter()

        result = service.run_sync(
            sync_name=sync_name,
            limit=limit,
            mode=mode,
        )

        elapsed = round(time.perf_counter() - batch_started, 2)
        inserted_or_updated = int(result.get("inserted_or_updated") or 0)

        batch_summary = {
            "batch_no": batch_no,
            "inserted_or_updated": inserted_or_updated,
            "last_id": result.get("last_id"),
            "last_timestamp": str(result.get("last_timestamp")),
            "elapsed_seconds": elapsed,
        }
        batches.append(batch_summary)

        total_inserted_or_updated += inserted_or_updated

        print(
            f"  batch={batch_no} "
            f"rows={inserted_or_updated} "
            f"last_id={result.get('last_id')} "
            f"last_ts={result.get('last_timestamp')} "
            f"time={elapsed}s"
        )

        # Done for this table.
        if inserted_or_updated == 0:
            break

        # If less than limit came back, source is caught up.
        if inserted_or_updated < limit:
            break

    else:
        status = "max_batches_reached"

    return {
        "sync_name": sync_name,
        "mode": mode,
        "limit": limit,
        "max_batches": max_batches,
        "status": status,
        "total_inserted_or_updated": total_inserted_or_updated,
        "batches": batches,
        "elapsed_seconds": round(time.perf_counter() - table_started, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Daily incremental sync for all AnalyticsEngine staging tables."
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-batches", type=int, default=20)
    parser.add_argument("--only", default=None, help="Comma-separated sync names to run")
    parser.add_argument("--skip", default=None, help="Comma-separated sync names to skip")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument(
        "--json-summary",
        default=None,
        help="Optional path to write JSON run summary.",
    )
    args = parser.parse_args()

    only = {
        item.strip()
        for item in str(args.only or "").split(",")
        if item.strip()
    }
    skip = {
        item.strip()
        for item in str(args.skip or "").split(",")
        if item.strip()
    }

    configs = DAILY_SYNC_TABLES
    if only:
        configs = [cfg for cfg in configs if cfg["sync_name"] in only]
    if skip:
        configs = [cfg for cfg in configs if cfg["sync_name"] not in skip]

    started_at = datetime.now()
    started = time.perf_counter()

    print("=" * 100)
    print("AnalyticsEngine daily staging sync")
    print("=" * 100)
    print(f"started_at={started_at.isoformat(sep=' ', timespec='seconds')}")
    print(f"tables={len(configs)}")
    print(f"default_max_batches={args.max_batches}")
    print()

    db_url = get_analytics_db_url()
    engine = create_engine(db_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()

    results = []
    failures = []

    try:
        service = AnalyticsStagingSyncService(db)

        for cfg in configs:
            sync_name = cfg["sync_name"]
            mode = cfg["mode"]
            limit = int(args.limit or cfg["limit"])

            print("-" * 100)
            print(f"Running {sync_name} | mode={mode} | limit={limit}")

            try:
                result = run_one_table(
                    service,
                    sync_name=sync_name,
                    mode=mode,
                    limit=limit,
                    max_batches=int(args.max_batches),
                )
                results.append(result)

                print(
                    f"Done {sync_name}: "
                    f"rows={result['total_inserted_or_updated']} "
                    f"status={result['status']} "
                    f"time={result['elapsed_seconds']}s"
                )

            except Exception as exc:
                db.rollback()
                failure = {
                    "sync_name": sync_name,
                    "mode": mode,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
                failures.append(failure)
                print(f"FAILED {sync_name}: {exc}")

                if args.fail_fast:
                    break

        summary = {
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now().isoformat(),
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "tables_attempted": len(results) + len(failures),
            "tables_success": len(results),
            "tables_failed": len(failures),
            "total_inserted_or_updated": sum(
                int(r.get("total_inserted_or_updated") or 0) for r in results
            ),
            "results": results,
            "failures": failures,
        }

        print()
        print("=" * 100)
        print("Daily staging sync summary")
        print("=" * 100)
        print(f"tables_success={summary['tables_success']}")
        print(f"tables_failed={summary['tables_failed']}")
        print(f"total_inserted_or_updated={summary['total_inserted_or_updated']}")
        print(f"elapsed_seconds={summary['elapsed_seconds']}")

        if failures:
            print()
            print("Failures:")
            for failure in failures:
                print(f"  - {failure['sync_name']}: {failure['error']}")

        if args.json_summary:
            out_path = Path(args.json_summary)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(summary, indent=2, default=str),
                encoding="utf-8",
            )
            print(f"\nJSON summary written: {out_path}")

        return 1 if failures else 0

    finally:
        try:
            db.close()
        finally:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
