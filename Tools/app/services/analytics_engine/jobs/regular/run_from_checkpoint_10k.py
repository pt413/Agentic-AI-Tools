# -----------------------------------------------------------------------------
# Repo bootstrap
# -----------------------------------------------------------------------------
from __future__ import annotations

import argparse
import inspect
import os
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text


def _analytics_engine_project_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent, *current.parents]:
        if (parent / "app").is_dir():
            return parent
    raise RuntimeError("Could not find FastApi project root containing the app/ folder.")


PROJECT_ROOT = _analytics_engine_project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import get_db  # noqa: E402
from app.services.analytics_engine.core.config import SCHEMA_NAME  # noqa: E402

from app.services.analytics_engine.processors.user_account_sync import UserAccountSync  # noqa: E402
from app.services.analytics_engine.processors.lead_sync import LeadSync  # noqa: E402
from app.services.analytics_engine.processors.call_log_sync import CallLogSync  # noqa: E402
from app.services.analytics_engine.processors.booking_confirm_sync import BookingConfirmSync  # noqa: E402
from app.services.analytics_engine.processors.site_visit_sync import SiteVisitSync  # noqa: E402
from app.services.analytics_engine.processors.travel_cart_sync import TravelCartSync  # noqa: E402
from app.services.analytics_engine.processors.web_visit_sync import WebVisitSync  # noqa: E402
from app.services.analytics_engine.processors.checkin_form_sync import CheckinFormSync  # noqa: E402
from app.services.analytics_engine.processors.checkout_form_sync import CheckoutFormSync  # noqa: E402
from app.services.analytics_engine.processors.email_message_sync import EmailMessageSync  # noqa: E402
from app.services.analytics_engine.processors.booking_audit_history_sync import BookingAuditHistorySync  # noqa: E402
from app.services.analytics_engine.processors.booking_invoice_details_sync import BookingInvoiceDetailsSync  # noqa: E402
from app.services.analytics_engine.processors.user_ticket_sync import UserTicketSync  # noqa: E402
from app.services.analytics_engine.processors.user_wishlist_sync import UserWishlistSync  # noqa: E402
from app.services.analytics_engine.processors.whatsapp_message_sync import WhatsAppMessageSync  # noqa: E402


LIMIT_PER_PROCESSOR = int(os.getenv("ANALYTICS_PROCESSOR_LIMIT", "10000"))
DEFAULT_CALL_LOG_BUILD_LIMIT = int(os.getenv("CALL_LOG_BUILD_LIMIT", "50000"))
DEFAULT_CALL_LOG_RESYNC_WINDOW = int(os.getenv("CALL_LOG_BUILD_RESYNC_WINDOW", "5000"))
STAGING_CALL_LOG_UNIFIED = f'"{SCHEMA_NAME}".staging_call_log_unified'


# These new staging-only tables from run_daily_stage_sync_all are intentionally
# not listed here because they do not have event processors yet:
#   admin_user_account, mobile_tagging/staff_phone_assignment,
#   building_details, property_unit.
# They should be refreshed by run_daily_stage_sync_all / run_stage_sync first.


def _csv_set(value: str | None) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def get_checkpoint(service, processor_name: str) -> dict[str, Any]:
    try:
        return service.get_checkpoint(processor_name) or {}
    except Exception:
        return {}


def run_unified_call_log_build(
    db,
    *,
    limit: int,
    resync_window: int,
    rebuild_all: bool = False,
    match_only: bool = False,
    skip_counts: bool = True,
    skip_duplicate_refresh: bool = True,
    skip_backfill_existing_keys: bool = True,
    skip_match: bool = False,
    diagnostics: bool = False,
    no_timing: bool = False,
) -> dict[str, Any]:
    """Build/refresh staging_call_log_unified before CallLogSync runs."""
    from app.services.analytics_engine.jobs.regular.build_unified_call_log import (  # noqa: WPS433
        UnifiedCallLogBuilder,
    )

    print("\n" + "=" * 100)
    print("Running: build_unified_call_log")
    print("=" * 100)
    print(f"limit           : {limit}")
    print(f"resync_window   : {resync_window}")
    print(f"rebuild_all     : {rebuild_all}")
    print(f"match_only      : {match_only}")
    print(f"skip_counts     : {skip_counts}")

    started = time.perf_counter()
    result = UnifiedCallLogBuilder(db, timing_enabled=not no_timing).run(
        limit=int(limit),
        resync_window=int(resync_window),
        rebuild_all=bool(rebuild_all),
        match_only=bool(match_only),
        diagnostics=bool(diagnostics),
        skip_counts=bool(skip_counts),
        refresh_duplicate_counts=not bool(skip_duplicate_refresh),
        backfill_existing_keys=not bool(skip_backfill_existing_keys),
        match_after_insert=not bool(skip_match),
    )
    elapsed = time.perf_counter() - started

    print("Result:")
    for key in ("raw_sync", "unified_build", "unified_counts", "match_diagnostics"):
        if key in result:
            print(f"  {key}: {result.get(key)}")
    print(f"Elapsed: {elapsed:.2f}s")
    return result


def ensure_call_log_fetch_compat(processor) -> None:
    """Patch older SourceIngestionService instances to accept last_timestamp.

    Some CallLogSync versions fetch unified call rows using a time cursor:
        fetch_call_logs(last_source_id=..., last_timestamp=..., batch_size=...)

    Older SourceIngestionService.fetch_call_logs() only accepted last_source_id
    and batch_size.  This compatibility shim keeps this runner usable even before
    the service file is updated.
    """
    source = getattr(processor, "source", None)
    fetch_call_logs = getattr(source, "fetch_call_logs", None)
    if source is None or fetch_call_logs is None:
        return



    def fetch_call_logs_compatible(last_source_id=0, batch_size=5000, last_timestamp=None):
        common_select = f"""
            SELECT
                source_id,
                executive_id,
                executive_name,
                call_time,
                talk_time_sec,
                call_direction,
                call_result,
                counterparty_phone,
                sales_phone,
                lead_id,
                department,
                audio_url,
                transcript_text,
                transcript_text_eleven_labs,
                translated_text,
                raw_transcripts,
                intent,
                emotion,
                tone,
                action_layer,
                context,
                outcome,
                language,
                priority,
                source_call_id,
                filename,
                uploaded_at,
                source_status,
                sync_status,
                synced_at,
                updated_at
            FROM {STAGING_CALL_LOG_UNIFIED}
        """

        if last_timestamp is not None:
            sql = common_select + """
            WHERE call_time IS NOT NULL
              AND (
                    COALESCE(updated_at, synced_at) > :last_timestamp
                    OR (
                        COALESCE(updated_at, synced_at) = :last_timestamp
                        AND source_id > :last_source_id
                    )
                  )
            ORDER BY COALESCE(updated_at, synced_at), source_id
            LIMIT :batch_size
            """
            return source.db.execute(
                text(sql),
                {
                    "last_source_id": int(last_source_id or 0),
                    "last_timestamp": last_timestamp,
                    "batch_size": int(batch_size),
                },
            ).fetchall()

        sql = common_select + """
        WHERE source_id > :last_source_id
          AND call_time IS NOT NULL
        ORDER BY source_id
        LIMIT :batch_size
        """
        return source.db.execute(
            text(sql),
            {
                "last_source_id": int(last_source_id or 0),
                "batch_size": int(batch_size),
            },
        ).fetchall()

    source.fetch_call_logs = fetch_call_logs_compatible


def run_processor(label, processor, batch_size, limit=LIMIT_PER_PROCESSOR):
    if label == "call_log_sync":
        ensure_call_log_fetch_compat(processor)

    checkpoint = get_checkpoint(processor.checkpoints, processor.processor_name)

    print("\n" + "=" * 100)
    print(f"Running: {label}")
    print("=" * 100)
    print(f"processor_name : {processor.processor_name}")
    print(f"source_table    : {processor.source_table}")
    print(f"batch_size      : {batch_size}")
    print(f"limit           : {limit}")
    print(
        "checkpoint      : "
        f"last_id={checkpoint.get('last_id')}, "
        f"last_timestamp={checkpoint.get('last_timestamp')}, "
        f"status={checkpoint.get('last_status')}, "
        f"updated_at={checkpoint.get('updated_at')}"
    )

    started = time.perf_counter()

    # start_source_id is intentionally NOT passed.
    # This makes each processor continue from its existing processor_checkpoint.
    result = processor.run(
        limit=limit,
        batch_size=batch_size,
    )

    elapsed = time.perf_counter() - started

    print("Result:")
    for k, v in result.items():
        print(f"  {k}: {v}")
    print(f"Elapsed: {elapsed:.2f}s")

    new_checkpoint = get_checkpoint(processor.checkpoints, processor.processor_name)
    print(
        "new_checkpoint  : "
        f"last_id={new_checkpoint.get('last_id')}, "
        f"last_timestamp={new_checkpoint.get('last_timestamp')}, "
        f"status={new_checkpoint.get('last_status')}, "
        f"updated_at={new_checkpoint.get('updated_at')}"
    )

    return result


def build_jobs(db) -> list[dict[str, Any]]:
    return [
        {
            "label": "user_account_sync",
            "processor": UserAccountSync(db),
            "batch_size": 2000,
        },
        {
            "label": "lead_sync",
            "processor": LeadSync(db),
            "batch_size": 2000,
        },
        {
            "label": "call_log_sync",
            "processor": CallLogSync(db),
            "batch_size": 2000,
        },
        {
            "label": "booking_confirm_sync",
            "processor": BookingConfirmSync(db),
            "batch_size": 2000,
        },
        {
            "label": "site_visit_sync",
            "processor": SiteVisitSync(db),
            "batch_size": 2000,
        },
        {
            "label": "travel_cart_sync",
            "processor": TravelCartSync(db),
            "batch_size": 2000,
        },
        {
            "label": "web_visit_sync",
            "processor": WebVisitSync(db),
            "batch_size": 2000,
        },
        {
            "label": "checkin_form_sync",
            "processor": CheckinFormSync(db),
            "batch_size": 1500,
        },
        {
            "label": "checkout_form_sync",
            "processor": CheckoutFormSync(db),
            "batch_size": 1500,
        },
        {
            "label": "email_message_sync",
            "processor": EmailMessageSync(db),
            "batch_size": 1500,
        },
        {
            "label": "booking_audit_history_sync",
            "processor": BookingAuditHistorySync(db),
            "batch_size": 2000,
        },
        {
            "label": "booking_invoice_details_sync",
            "processor": BookingInvoiceDetailsSync(db),
            "batch_size": 1500,
        },
        {
            "label": "user_ticket_sync",
            "processor": UserTicketSync(db),
            "batch_size": 1500,
        },
        {
            "label": "user_wishlist_sync",
            "processor": UserWishlistSync(db),
            "batch_size": 2000,
        },
        {
            "label": "whatsapp_message_sync",
            "processor": WhatsAppMessageSync(db),
            "batch_size": 1500,
        },
    ]


def filter_jobs(jobs: list[dict[str, Any]], *, only: set[str], skip: set[str]) -> list[dict[str, Any]]:
    if only:
        jobs = [job for job in jobs if job["label"] in only]
    if skip:
        jobs = [job for job in jobs if job["label"] not in skip]
    return jobs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run AnalyticsEngine event processors from existing processor checkpoints."
    )
    parser.add_argument("--limit", type=int, default=LIMIT_PER_PROCESSOR)
    parser.add_argument("--only", default=None, help="Comma-separated processor labels to run")
    parser.add_argument("--skip", default=None, help="Comma-separated processor labels to skip")
    parser.add_argument("--skip-call-log-build", action="store_true")
    parser.add_argument("--call-log-build-limit", type=int, default=DEFAULT_CALL_LOG_BUILD_LIMIT)
    parser.add_argument("--call-log-resync-window", type=int, default=DEFAULT_CALL_LOG_RESYNC_WINDOW)
    parser.add_argument("--call-log-rebuild-all", action="store_true")
    parser.add_argument("--call-log-match-only", action="store_true")
    parser.add_argument("--call-log-skip-counts", action="store_true", default=True)
    parser.add_argument("--call-log-include-counts", action="store_false", dest="call_log_skip_counts")
    parser.add_argument("--call-log-skip-duplicate-refresh", action="store_true", default=True)
    parser.add_argument("--call-log-refresh-duplicates", action="store_false", dest="call_log_skip_duplicate_refresh")
    parser.add_argument("--call-log-skip-backfill-existing-keys", action="store_true", default=True)
    parser.add_argument("--call-log-backfill-existing-keys", action="store_false", dest="call_log_skip_backfill_existing_keys")
    parser.add_argument("--call-log-skip-match", action="store_true")
    parser.add_argument("--call-log-diagnostics", action="store_true")
    parser.add_argument("--no-timing", action="store_true")
    args = parser.parse_args()

    db_gen = get_db()
    db = next(db_gen)

    try:
        only = _csv_set(args.only)
        skip = _csv_set(args.skip)
        jobs = filter_jobs(build_jobs(db), only=only, skip=skip)
        selected_labels = {job["label"] for job in jobs}

        print("=" * 100)
        print(f"AnalyticsEngine checkpoint runner: up to {int(args.limit)} rows per processor")
        print("=" * 100)
        print(f"processors={', '.join(label for label in selected_labels) if selected_labels else '-'}")

        # The call-log processor now reads staging_call_log_unified.  Refresh it
        # before CallLogSync, otherwise the processor may see stale call rows.
        if "call_log_sync" in selected_labels and not args.skip_call_log_build:
            run_unified_call_log_build(
                db,
                limit=int(args.call_log_build_limit),
                resync_window=int(args.call_log_resync_window),
                rebuild_all=bool(args.call_log_rebuild_all),
                match_only=bool(args.call_log_match_only),
                skip_counts=bool(args.call_log_skip_counts),
                skip_duplicate_refresh=bool(args.call_log_skip_duplicate_refresh),
                skip_backfill_existing_keys=bool(args.call_log_skip_backfill_existing_keys),
                skip_match=bool(args.call_log_skip_match),
                diagnostics=bool(args.call_log_diagnostics),
                no_timing=bool(args.no_timing),
            )
        elif "call_log_sync" in selected_labels:
            print("\nSkipping build_unified_call_log because --skip-call-log-build was supplied.")

        for job in jobs:
            run_processor(
                label=job["label"],
                processor=job["processor"],
                batch_size=job["batch_size"],
                limit=int(args.limit),
            )

        print("\nDone.")
        return 0

    finally:
        try:
            db.close()
        except Exception:
            pass
        try:
            next(db_gen, None)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
