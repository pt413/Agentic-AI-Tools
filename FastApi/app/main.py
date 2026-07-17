import os

# ---------------------------------------------------------------------
# HuggingFace / Transformers startup-noise controls
# Must be set before any route/service import that may import transformers.
# ---------------------------------------------------------------------
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import sys
import signal
import logging
import asyncio
import secrets
import warnings
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

warnings.filterwarnings(
    "ignore",
    message=r".*Using a slow image processor as `use_fast` is unset.*",
)

try:
    from transformers.utils import logging as transformers_logging

    if os.getenv("TRANSFORMERS_STARTUP_LOG_LEVEL", "error").strip().lower() == "error":
        transformers_logging.set_verbosity_error()
except Exception:
    pass

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.db.database import SessionLocal
from app.db.create_tables import create_tables_once
from app.setup.intent_loader import load_predefined_intents
from app.cron_jobs import start_scheduler
from app.metrics import metrics_middleware
#from app.routes.user_data_activity_route import router as user_data_activity_router
# from app.routes.conversations import router as conversations_router
from app.routes.whatsapp_route import router as whatsapp_router
# from app.routes.email_fetch_router import router as email_fetch_router
#from app.routes.conversations import router as conversations_router
# from app.routes.whatsapp_route import router as whatsapp_router
# from app.routes.email_fetch_router import router as email_fetch_router
#from app.routes.email_classification_demo_route import router as email_risk_classifier_router


from app.routes.audio_route import router as audio_router
# from app.routes.emails_routes import router as emails_router
#from app.routes.call_log import router as calls_router
# from app.routes.bp_ai_route import router as bp_ai_router
# from app.routes.messages_route import router as messages_router
# from app.routes.auth_route import router as auth_router
# from app.routes.summary_route import router as summary_router
#from app.routes.transcribe_route import router as transcribe_router
# from app.routes.pinecone_emails import router as pinecone_router
# from app.routes.pinecone_hybrid_emails import router as pinecone_hybrid_router
# from app.routes.files_rag import router as files_rag_router
#from app.routes.rag_route import router as rag__router
#from app.routes.geo_route import router as geo_router
# from app.routes.adverse_lookup_route import router as adverse_route
#from app.routes.files_rag import router as files_rag_router
# from app.routes.rag_route import router as rag__router
from app.routes.google_contact_route import router as google_contact_router
#from app.routes.geo_route import router as geo_router
#from app.routes.adverse_lookup_route import router as adverse_route
# from app.routes.organization_router import router as organization_router
from app.routes.browse_history_route import router as browse_history_router
# from app.dependencies import shutdown_background_db
from app.routes.url_route import router as url_router
from app.routes.email_route import router as email_router
from app.routes.call_log import router as call_log_router
from app.routes.transcribe_route import router as transcribe_router
# Platform utilities (from the refactor)
# from app.platform.tenant.logging_config import init_logging
# from app.platform.tenant.registry_factory import DEFAULT_REGISTRY_FACTORY, ConnectorDescriptor, InMemoryConnectorRegistry
# from app.platform.tenant.tenants.rentmystay import manifest as rentmystay_manifest
# import app.BrightpathAI.models
# from app.routes.debug_route import router as debug_router
# from app.routes.debug_manifest_route import router as debug_manifest_router
# from app.routes.tenant_debug_route import router as tenant_debug_router
# from app.routes.ocr_route import router as ocr_router
# from app.routes.session_route import router as session_router
# from app.platform.tenant.middleware import RequestContextASGIMiddleware
# from app.platform.tenant.tenants.rentmystay.api_client import get_client

# initialize logging
# init_logging()
# logger = logging.getLogger("app.main")

from app.utils.logger import get_logger
from app.metrics import metrics_middleware
from app.routes.analytics_capabilities import (
    router as analytics_capability_router,
)

# --------------------------------------------
# Logging
# --------------------------------------------
log = get_logger("main", level=logging.INFO)

# --------------------------------------------
# Temporary simple login / HTTP Basic Auth
# --------------------------------------------
# Env flags:
#   SIMPLE_AUTH_ENABLED=1
#   SIMPLE_AUTH_USER=admin
#   SIMPLE_AUTH_PASSWORD=change-this-password
#
# Set SIMPLE_AUTH_ENABLED=0 to disable this temporary auth layer.
basic_auth = HTTPBasic(auto_error=False)

SIMPLE_AUTH_ENABLED = os.getenv("SIMPLE_AUTH_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
SIMPLE_AUTH_USER = os.getenv("SIMPLE_AUTH_USER", "admin")
SIMPLE_AUTH_PASSWORD = os.getenv("SIMPLE_AUTH_PASSWORD", "admin123")


def require_basic_auth(
    credentials: Optional[HTTPBasicCredentials] = Depends(basic_auth),
):
    if not SIMPLE_AUTH_ENABLED:
        return None

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Login required",
            headers={"WWW-Authenticate": "Basic"},
        )

    username_ok = secrets.compare_digest(credentials.username, SIMPLE_AUTH_USER)
    password_ok = secrets.compare_digest(credentials.password, SIMPLE_AUTH_PASSWORD)

    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username


AUTH_DEPS = [Depends(require_basic_auth)]

# --------------------------------------------
# Startup task tracking
# --------------------------------------------
_background_startup_tasks: list[asyncio.Task] = []
_scheduler_started = False

def _track_task(task: asyncio.Task) -> None:
    _background_startup_tasks.append(task)

    def _cleanup_done_task(done_task: asyncio.Task) -> None:
        try:
            _background_startup_tasks.remove(done_task)
        except ValueError:
            pass

        try:
            exc = done_task.exception()
            if exc:
                log.exception("Background startup task failed", exc_info=exc)
        except asyncio.CancelledError:
            log.info("Background startup task cancelled")

    task.add_done_callback(_cleanup_done_task)


async def _run_db_setup() -> None:
    try:
        from app.db.create_tables import create_tables_once

        await asyncio.to_thread(create_tables_once)
        log.info("✅ Database tables ready")
    except Exception:
        log.exception("❌ Database setup failed")


async def _load_predefined_intents() -> None:
    db = None
    try:
        from app.db.database import SessionLocal
        from app.setup.intent_loader import load_predefined_intents

        db = SessionLocal()
        await load_predefined_intents(db)
        log.info("✅ Predefined intents loaded")
    except Exception:
        log.exception("❌ Intent loading failed")
    finally:
        if db:
            db.close()


async def _restore_whatsapp_sessions() -> None:
    try:
        from app.services.whatsapp_service import restore_active_whatsapp_sessions

        await asyncio.to_thread(restore_active_whatsapp_sessions)
        log.info("✅ WhatsApp sessions restored")
    except Exception:
        log.exception("❌ WhatsApp session restoration failed")

def _start_scheduler_once() -> None:
    global _scheduler_started
    if _scheduler_started:
        return

    try:
        from app.cron_jobs import start_scheduler

        start_scheduler()
        _scheduler_started = True
        log.info("✅ Scheduler started")
    except Exception:
        log.exception("❌ Scheduler failed")


async def _warmup_non_critical_services() -> None:
    """
    Non-critical startup work.
    These should not block FastAPI boot on local development.
    """
    await asyncio.gather(
        _load_predefined_intents(),
        _restore_whatsapp_sessions(),
        return_exceptions=True,
    )


# # --------------------------------------------
# # Lifespan
# # --------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🚀 Starting application...")

    # # Critical startup only
    await _run_db_setup()

    if os.getenv("ENABLE_APSCHEDULER", "false").strip().lower() in {"1", "true", "yes", "on"}:
        _start_scheduler_once()
    else:
        log.info("⏭️ Scheduler disabled (ENABLE_APSCHEDULER is not true)")

    # # Non-critical tasks run in background so app responds faster
    startup_task = asyncio.create_task(_warmup_non_critical_services())
    _track_task(startup_task)

    log.info("🎉 Application startup complete")

    yield

    log.info("🛑 Shutting down application...")

    # # Cancel any still-running startup background tasks cleanly
    for task in list(_background_startup_tasks):
        if not task.done():
            task.cancel()

    if _background_startup_tasks:
        await asyncio.gather(*_background_startup_tasks, return_exceptions=True)

    # # Optional cleanup
    try:
        import app.dependencies as dependencies

        shutdown_background_db = getattr(dependencies, "shutdown_background_db", None)
        if callable(shutdown_background_db):
            result = shutdown_background_db()
            if asyncio.iscoroutine(result):
                await result
    except Exception:
        log.exception("❌ Background DB shutdown failed")

    log.info("👋 Application shutdown complete")


# # --------------------------------------------
# # FastAPI app
# # --------------------------------------------
app = FastAPI(
    title="BP AI Backend",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# # --------------------------------------------
# # CORS
# # --------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://gpt.bpai.in",
        "https://gpt.bpai.in",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5175",
        "http://localhost:5175",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# # --------------------------------------------
# # Middleware
# # --------------------------------------------
app.middleware("http")(metrics_middleware)


# # --------------------------------------------
# # Health
# # --------------------------------------------
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "bp-ai-backend",
    }


@app.get("/ready")
async def readiness():
    checks = {
        "database": False,
        "node": False,
        "filesystem": False,
    }

    # # 1. Database
    try:
        from app.db.database import SessionLocal
        from sqlalchemy import text

        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            checks["database"] = True
        finally:
            db.close()
    except Exception:
        pass

    # # 2. Node.js
    try:
        import subprocess

        node_ver = await asyncio.to_thread(
            subprocess.check_output, ["node", "--version"], text=True
        )
        if node_ver.strip():
            checks["node"] = True
    except Exception:
        pass

    # # 3. Filesystem
    try:
        from app.services import whatsapp_service

        services_dir = os.path.dirname(os.path.abspath(whatsapp_service.__file__))
        auth_dir = os.path.join(services_dir, ".baileys_auth")
        os.makedirs(auth_dir, exist_ok=True)
        if os.access(auth_dir, os.W_OK):
            checks["filesystem"] = True
    except Exception:
        pass

    all_healthy = all(checks.values())

    return {
        "status": "ready" if all_healthy else "not_ready",
        "checks": checks,
        "timestamp": datetime.utcnow().isoformat(),
    }


# --------------------------------------------
# Router registration
# Keep imports here, but remove duplicates.
# Health/docs stay open; app/API routers are protected with AUTH_DEPS.
# --------------------------------------------
# from app.routes.user_data_activity_route import router as user_data_activity_router
# from app.routes.timeline_access_route import router as timeline_access_router
# from app.routes.whatsapp_route import router as whatsapp_router
# from app.routes.email_fetch_router import router as email_fetch_router
# from app.routes.audio_route import router as audio_router
# from app.routes.emails_routes import router as emails_router
# from app.routes.call_log import router as calls_router
# from app.routes.messages_route import router as messages_router
# from app.routes.summary_route import router as summary_router
# from app.routes.transcribe_route import router as transcribe_router
# from app.routes.files_rag import router as files_rag_router
from app.routes.rag_route import router as rag_router
from app.routes.geo_route import router as geo_router
from app.routes.google_contact_route import router as google_contact_router
# from app.routes.organization_router import router as organization_router
# from app.routes.browse_history_route import router as browse_history_router
# from app.routes.url_route import router as url_router
# from app.routes.email_route import router as email_router
# from app.routes.analytics_capabilities.conversation_route import router as conversation_router
# from app.routes.analytics_capabilities.staff_routes import router as staff_routers
# from app.routes.ocr_route import router as ocr_router
# from app.routes.ocr_route import router as ocr_router

# app.include_router(email_fetch_router, dependencies=AUTH_DEPS)
# app.include_router(user_data_activity_router, dependencies=AUTH_DEPS)
# app.include_router(emails_router, dependencies=AUTH_DEPS)
# app.include_router(calls_router, dependencies=AUTH_DEPS)
# app.include_router(messages_router, dependencies=AUTH_DEPS)
# app.include_router(timeline_access_router, dependencies=AUTH_DEPS)
app.include_router(whatsapp_router, prefix="/connector", dependencies=AUTH_DEPS)
app.include_router(summary_router, dependencies=AUTH_DEPS)
app.include_router(transcribe_router, prefix="/connector", dependencies=AUTH_DEPS)
app.include_router(files_rag_router, prefix="/connector", dependencies=AUTH_DEPS)
app.include_router(email_router, prefix="/connector", dependencies=AUTH_DEPS)
app.include_router(audio_router, prefix="/connector", dependencies=AUTH_DEPS)
app.include_router(url_router, prefix="/connector", dependencies=AUTH_DEPS)
app.include_router(browse_history_router, prefix="/connector", dependencies=AUTH_DEPS)
app.include_router(organization_router, dependencies=AUTH_DEPS)
app.include_router(google_contact_router, dependencies=AUTH_DEPS)
app.include_router(rag_router, dependencies=AUTH_DEPS)
app.include_router(geo_router, dependencies=AUTH_DEPS)
app.include_router(analytics_capability_router, dependencies=AUTH_DEPS)
app.include_router(conversation_router)
app.include_router(staff_routers, dependencies=AUTH_DEPS)
# app.include_router(ocr_router, dependencies=AUTH_DEPS)

# --------------------------------------------
# Signals
# --------------------------------------------
# -------------------------------------------------------
# Startup Events (Alternative to lifespan - commented)
# -------------------------------------------------------
# @app.on_event("startup")
# def on_startup():
#     start_scheduler()

# @app.on_event("startup")
# def on_startup():
#     start_scheduler_k()


# -------------------------------------------------------
# Include Routers
# -------------------------------------------------------
# app.include_router(email_fetch_router)
# app.include_router(user_data_activity_router)
# app.include_router(conversations_router)
# app.include_router(proxy_router)
# app.include_router(files_router)
# app.include_router(emails_router)
# app.include_router(calls_router)
# app.include_router(bp_ai_router)
# app.include_router(messages_router)
# app.include_router(whatsapp_router,prefix="/connector")
# app.include_router(auth_router)
# app.include_router(summary_router)
# app.include_router(transcribe_router,prefix="/connector")
# app.include_router(pinecone_router)
# app.include_router(pinecone_hybrid_router)
# app.include_router(files_rag_router)
#app.include_router(files_rag_router,prefix="/connector")
# app.include_router(metrics_router)
# app.include_router(email_router,prefix="/connector")
# app.include_router(refund_check_router)
# Routes for data display and management
# app.include_router(audio_router,prefix="/connector")
#app.include_router(email_router)
# app.include_router(call_log_router,prefix="/connector")
# app.include_router(url_router,prefix="/connector")
# app.include_router(organization_router)
# app.include_router(wa_router)
# app.include_router(browse_history_router,prefix="/connector")
# app.include_router(debug_router)
# app.include_router(debug_manifest_router)
# app.include_router(tenant_debug_router, prefix="")
# app.include_router(session_router)
app.include_router(rag_router)
app.include_router(google_contact_router)
app.include_router(geo_router)
# app.include_router(adverse_route)
# Now wrap the app with the RequestContextASGIMiddleware ASGI wrapper. This keeps
# add_middleware() calls valid and ensures the request-context middleware is applied.
# app.add_middleware(RequestContextASGIMiddleware)
# app.include_router(ocr_router)

# logger.info("App routes registered")


# ============================================
# SIGNAL HANDLERS
# ============================================
def shutdown_handler(signum, frame):
    log.info("Shutdown signal received (%s). Exiting...", signum)
    sys.exit(0)


# # --------------------------------------------
# # Main entrypoint
# # --------------------------------------------
if __name__ == "__main__":
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    reload = os.getenv("RELOAD", "false").lower() == "true"
    workers = int(os.getenv("WORKERS", "1"))

    log.info("🚀 Starting server on %s:%s", host, port)

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        log_level="info",
        reload=reload,
        workers=1 if reload else workers,
    )
