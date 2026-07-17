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

import importlib
import os
import sys
import traceback
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


# Ensure repo root is on sys.path
REPO_ROOT = os.path.abspath(os.path.dirname(__file__))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def get_db_url() -> str:
    db_url = os.getenv("DATABASE_URL") or os.getenv("PG_URL")
    if db_url:
        return db_url

    try:
        from dotenv import load_dotenv
        load_dotenv()
        db_url = os.getenv("DATABASE_URL") or os.getenv("PG_URL")
        if db_url:
            return db_url
    except Exception:
        pass

    try:
        from app.database.session import DATABASE_URL
        if DATABASE_URL:
            return DATABASE_URL
    except Exception:
        pass

    raise RuntimeError("DATABASE_URL / PG_URL not found")


PROCESSOR_SPECS = [
    ("site_visits", "app.services.analytics_engine.processors.site_visit_sync", "SiteVisitSync"),
    ("travel_cart", "app.services.analytics_engine.processors.travel_cart_sync", "TravelCartSync"),
    ("user_wishlist", "app.services.analytics_engine.processors.user_wishlist_sync", "UserWishlistSync"),
    ("whatsapp_messages", "app.services.analytics_engine.processors.whatsapp_message_sync", "WhatsAppMessageSync"),
    ("web_visits", "app.services.analytics_engine.processors.web_visit_sync", "WebVisitSync"),
    ("checkin_form", "app.services.analytics_engine.processors.checkin_form_sync", "CheckinFormSync"),
    ("checkout_form", "app.services.analytics_engine.processors.checkout_form_sync", "CheckoutFormSync"),
    ("user_ticket", "app.services.analytics_engine.processors.user_ticket_sync", "UserTicketSync"),
    ("email_messages", "app.services.analytics_engine.processors.email_message_sync", "EmailMessageSync"),
    ("booking_audit_history", "app.services.analytics_engine.processors.booking_audit_history_sync", "BookingAuditHistorySync"),
    ("booking_invoice_details", "app.services.analytics_engine.processors.booking_invoice_details_sync", "BookingInvoiceDetailsSync"),
]


def load_processor_class(module_name: str, class_name: str):
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def scalar(db, sql: str, params=None):
    return db.execute(text(sql), params or {}).scalar()


def count_events_for_source(db, source_table: str) -> int:
    return scalar(
        db,
        """
        SELECT COUNT(*)
        FROM "AnalyticsEngine".event_fact
        WHERE source_table = :source_table
        """,
        {"source_table": source_table},
    )


def get_checkpoint(db, processor_name: str):
    row = db.execute(
        text(
            """
            SELECT processor_name, source_table, last_id, last_timestamp,
                   last_status, last_batch_count, updated_at, last_error, notes
            FROM "AnalyticsEngine".processor_checkpoint
            WHERE processor_name = :processor_name
            """
        ),
        {"processor_name": processor_name},
    ).mappings().fetchone()
    return dict(row) if row else {}


def safe_source_table(sync_obj) -> str:
    return getattr(sync_obj, "SOURCE_TABLE_NAME", None) or getattr(sync_obj, "SOURCE_TABLE")


def run_one(session_factory, name: str, cls):
    db = session_factory()
    try:
        sync = cls(db)
        source_table = safe_source_table(sync)
        processor_name = sync.processor_name

        before_count = count_events_for_source(db, source_table)
        result = sync.run(limit=200, batch_size=100)
        after_count = count_events_for_source(db, source_table)
        checkpoint = get_checkpoint(db, processor_name)

        return {
            "name": name,
            "ok": True,
            "processor_name": processor_name,
            "source_table": source_table,
            "before_count": before_count,
            "after_count": after_count,
            "delta": after_count - before_count,
            "checkpoint": checkpoint,
            "result": result,
        }
    except Exception as exc:
        db.rollback()
        return {
            "name": name,
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    finally:
        db.close()


def main():
    print("=" * 100)
    print("Analytics Engine smoke test")
    print(f"Repo root: {REPO_ROOT}")
    print(f"Run time : {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 100)

    missing_modules = []
    loaded = []

    for name, module_name, class_name in PROCESSOR_SPECS:
        try:
            cls = load_processor_class(module_name, class_name)
            loaded.append((name, cls))
            print(f"[OK] import {module_name}.{class_name}")
        except Exception as exc:
            missing_modules.append((name, module_name, class_name, str(exc)))
            print(f"[MISSING] {module_name}.{class_name} -> {exc}")

    if missing_modules:
        print("\nThe following processor modules/classes are missing from your repo:")
        for name, module_name, class_name, err in missing_modules:
            print(f" - {name}: {module_name}.{class_name}")
        print("\nCopy those files into app/services/analytics_engine/sync first, then rerun.")
        sys.exit(2)

    engine = create_engine(get_db_url(), pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    failures = []
    passes = []

    for name, cls in loaded:
        print(f"\n--- Running {name} ---")
        out = run_one(SessionLocal, name, cls)
        if out["ok"]:
            passes.append(out)
            cp = out["checkpoint"]
            print(f"processor_name : {out['processor_name']}")
            print(f"source_table   : {out['source_table']}")
            print(f"delta_events   : {out['delta']}")
            print(f"last_status    : {cp.get('last_status')}")
            print(f"last_batch_cnt : {cp.get('last_batch_count')}")
            print(f"result         : {out['result']}")
        else:
            failures.append(out)
            print(f"FAILED: {out['error']}")
            print(out["traceback"])

    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print(f"Passed: {len(passes)}")
    print(f"Failed: {len(failures)}")

    if failures:
        sys.exit(1)

    print("All smoke tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()

