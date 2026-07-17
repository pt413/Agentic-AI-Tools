import asyncio
import logging
import os
import subprocess
import threading
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db.database import SessionLocal
from app.routes.lead_history_router import _sync_latest_data_internal
from app.routes.transcribe_route import run_pending_transcription
from app.services.email_risk_pipeline import run_pipeline
from app.services.rag_ingestion import ingest_all_tables


logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None

# Used to prevent the caretaker batch from starting while the
# one-minute analytics synchronization is currently running.
_analytics_sync_running = threading.Event()


def run_pending_transcriptions():
    db = None

    try:
        db = SessionLocal()
        logger.info("🕒 Cron: checking pending transcription tasks...")

        asyncio.run(run_pending_transcription(db))

        logger.info("✅ Pending transcription cron completed")

    except Exception as exc:
        logger.exception(
            "❌ Error running transcription cron: %s",
            exc,
        )

    finally:
        if db:
            db.close()


def _run_rag_ingestion_job():
    db = None

    try:
        db = SessionLocal()
        logger.info("🕒 Cron: Running RAG ingestion")

        summary = ingest_all_tables(db)

        logger.info(
            "✅ RAG ingestion completed: %s",
            summary,
        )

    except Exception:
        logger.exception("❌ RAG ingestion failed")

    finally:
        if db:
            db.close()


def _run_email_risk_job():
    try:
        logger.info("🕒 Cron: Running Email Risk Pipeline")

        run_pipeline()

        logger.info("✅ Email Risk Pipeline completed")

    except Exception:
        logger.exception("❌ Email Risk cron failed")


def _run_lead_history_sync_job():
    db = None

    try:
        db = SessionLocal()
        logger.info("🕒 Cron: Running lead history sync")

        inserted = _sync_latest_data_internal(db)

        logger.info(
            "✅ Lead history sync completed: %s",
            inserted,
        )

    except Exception:
        logger.exception("❌ Lead history cron failed")

    finally:
        if db:
            db.close()


def _run_analytics_daily_incremental_job():
    """
    Runs the analytics incremental synchronization.

    This intentionally remains scheduled every minute because other
    application processes depend on frequent synchronization.
    """

    script_path = os.getenv(
        "ANALYTICS_DAILY_SCRIPT",
        (
            "/home/bpai/BP_AI/FastApi/app/services/"
            "analytics_engine/scripts/run_daily_analytics_incremental.sh"
        ),
    )

    app_dir = os.getenv(
        "APP_DIR",
        "/home/bpai/BP_AI/FastApi",
    )

    timeout_seconds = int(
        os.getenv(
            "ANALYTICS_DAILY_TIMEOUT_SECONDS",
            str(60 * 60 * 4),
        )
    )

    if _analytics_sync_running.is_set():
        logger.warning(
            "⚠️ AnalyticsEngine incremental sync is already running; "
            "skipping this execution."
        )
        return

    _analytics_sync_running.set()

    try:
        logger.info(
            "🕒 Cron: Running AnalyticsEngine daily incremental sync"
        )

        result = subprocess.run(
            ["/bin/bash", script_path],
            cwd=app_dir,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )

        if result.returncode != 0:
            logger.error(
                "❌ AnalyticsEngine daily incremental sync failed "
                "with exit code %s",
                result.returncode,
            )
            return

        logger.info(
            "✅ AnalyticsEngine daily incremental sync completed"
        )

    except subprocess.TimeoutExpired:
        logger.exception(
            "❌ AnalyticsEngine daily incremental sync timed out"
        )

    except Exception:
        logger.exception(
            "❌ AnalyticsEngine daily incremental sync crashed"
        )

    finally:
        _analytics_sync_running.clear()


def _wait_for_analytics_sync() -> bool:
    """
    Wait for a currently running analytics synchronization to finish.

    Returns False if the configured wait timeout is exceeded.
    """

    timeout_seconds = int(
        os.getenv(
            "CARETAKER_WAIT_FOR_ANALYTICS_SECONDS",
            "1800",
        )
    )

    poll_seconds = 2
    started = time.monotonic()

    while _analytics_sync_running.is_set():
        elapsed = time.monotonic() - started

        if elapsed >= timeout_seconds:
            logger.error(
                "❌ Caretaker performance cron skipped because the "
                "analytics synchronization did not finish within %s seconds",
                timeout_seconds,
            )
            return False

        logger.info(
            "⏳ Caretaker performance cron is waiting for the "
            "analytics synchronization to finish"
        )

        time.sleep(poll_seconds)

    return True


def _run_caretaker_performance_30d_job():
    """
    Refresh the 30-day caretaker performance cache.

    The job:

    - Processes active caretakers only.
    - Waits for an active analytics synchronization to finish.
    - Uses existing cache when the evidence is unchanged.
    - Calls LLM when evidence has changed.
    - Does not force-refresh every caretaker.
    """

    from app.services.analytics_engine.capabilities.caretaker_performance.jobs import (
        rate_caretakers_batch,
    )

    if not _wait_for_analytics_sync():
        return

    db = None

    try:
        db = SessionLocal()

        logger.info(
            "🕒 Cron: Running caretaker performance 30-day refresh"
        )

        result = rate_caretakers_batch(
            db=db,
            schema=os.getenv(
                "CARETAKER_PERFORMANCE_SCHEMA",
                "AnalyticsEngine",
            ),
            days=30,
            active=True,
            limit=int(
                os.getenv(
                    "CARETAKER_PERFORMANCE_BATCH_LIMIT",
                    "500",
                )
            ),
            model=os.getenv(
                "CARETAKER_PERFORMANCE_MODEL",
                "gpt-5-mini",
            ),
            timeout_seconds=int(
                os.getenv(
                    "CARETAKER_PERFORMANCE_TIMEOUT_SECONDS",
                    "120",
                )
            ),
            run_llm=True,
            force_refresh=False,
            sleep_seconds=float(
                os.getenv(
                    "CARETAKER_PERFORMANCE_SLEEP_SECONDS",
                    "0",
                )
            ),
            fail_fast=False,
        )

        logger.info(
            "✅ Caretaker performance cron completed: "
            "requested=%s processed=%s skipped=%s refreshed=%s "
            "llm_calls=%s errors=%s",
            result.get("requested"),
            result.get("processed"),
            result.get("skipped_count"),
            result.get("refreshed_count"),
            result.get("llm_call_count"),
            result.get("error_count"),
        )

    except Exception:
        logger.exception(
            "❌ Caretaker performance cron failed"
        )

    finally:
        if db:
            db.close()


def run_email_sync():
    db = None

    try:
        logger.info(
            "📧 CRON JOB: Fetching all emails..."
        )

        from app.services.email_service import EmailService

        db = SessionLocal()
        service = EmailService(db)

        result = service.fetch_all_accounts()

        logger.info(
            "✅ Email sync completed: %s",
            result,
        )

    except Exception as exc:
        logger.exception(
            "❌ Email sync failed: %s",
            exc,
        )

    finally:
        if db:
            db.close()


def start_scheduler():
    global _scheduler

    if _scheduler and _scheduler.running:
        logger.info(
            "Scheduler already running — skipping"
        )
        return _scheduler

    _scheduler = BackgroundScheduler(
        timezone="Asia/Kolkata"
    )

    # Transcription
    _scheduler.add_job(
        run_pending_transcriptions,
        trigger=CronTrigger(hour="*/14"),
        id="run_pending_transcription",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # RAG ingestion
    _scheduler.add_job(
        _run_rag_ingestion_job,
        trigger=CronTrigger(hour="*/2"),
        id="run_rag_ingestion",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Email risk pipeline
    # _scheduler.add_job(
    #     _run_email_risk_job,
    #     trigger=CronTrigger(minute="*/45"),
    #     id="run_email_risk_pipeline",
    #     replace_existing=True,
    #     max_instances=1,
    # )

    # Lead history sync
    # _scheduler.add_job(
    #     _run_lead_history_sync_job,
    #     trigger=CronTrigger(minute="*/15"),
    #     id="run_lead_history_sync",
    #     replace_existing=True,
    #     max_instances=1,
    # )

    # Analytics incremental sync.
    # Keep this at one-minute intervals.
    _scheduler.add_job(
        _run_analytics_daily_incremental_job,
        trigger=CronTrigger(minute="*/1"),
        id="run_analytics_daily_incremental",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60 * 60,
    )

    # Caretaker performance 30-day refresh.
    # Starting at second 30 avoids launching at the exact same moment
    # as the one-minute analytics synchronization.
    _scheduler.add_job(
        _run_caretaker_performance_30d_job,
        trigger=CronTrigger(
            hour=2,
            minute=30,
            second=30,
        ),
        id="run_caretaker_performance_30d",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60 * 60,
    )

    # Email sync
    _scheduler.add_job(
        run_email_sync,
        trigger=CronTrigger(minute="*/30"),
        id="run_email_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    _scheduler.start()

    logger.info(
        "🕒 APScheduler started successfully"
    )

    return _scheduler