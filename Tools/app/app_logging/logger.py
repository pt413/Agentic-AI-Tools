# app/logging/logger.py

import logging
import json
import structlog
from typing import Optional

# ============================================================
# 1️⃣ BASE PYTHON LOGGING (NO FORMAT, NO PREFIX)
# ============================================================
# Let structlog fully control output.
logging.basicConfig(
    level=logging.INFO,
    handlers=[logging.StreamHandler()],
)

# Silence noisy third-party loggers (optional but recommended)
for noisy in (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "asyncio",
):
    logging.getLogger(noisy).propagate = False


# ============================================================
# 2️⃣ CUSTOM JSON RENDERER (EVENT ALWAYS FIRST)
# ============================================================

class JSONRendererEventFirst:
    """
    Ensures:
    - 'event' is always the FIRST key
    - Stable ordering
    - Clean JSON (1 line per log)
    """

    def __call__(self, logger, name, event_dict):
        event = event_dict.pop("event", "")

        ordered = {
            "event": event,
            **event_dict,
        }

        return json.dumps(
            ordered,
            ensure_ascii=False,
            separators=(",", ":"),
        )+"\n"


# ============================================================
# 3️⃣ STRUCTLOG CONFIGURATION (CANONICAL)
# ============================================================

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", key="timestamp"),
        JSONRendererEventFirst(),   # 🔑 EVENT FIRST
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)


# ============================================================
# 4️⃣ PUBLIC LOGGER API (ONLY THIS SHOULD BE USED)
# ============================================================

def get_logger(
    name: Optional[str] = None,
    **bind_fields,
):
    """
    Canonical logger factory.

    Usage:
        logger = get_logger(__name__)
        logger.info("startup.complete", version="1.2.3")

        logger = get_logger("planner", tenant_id="rentmystay")
        logger.info("planner.start", request_id=req_id)
    """
    log = structlog.get_logger(name)
    if bind_fields:
        log = log.bind(**bind_fields)
    return log


# ============================================================
# 5️⃣ DEFAULT ROOT LOGGER (OPTIONAL IMPORT)
# ============================================================

logger = get_logger()
