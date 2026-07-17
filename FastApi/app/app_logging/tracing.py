import os
import logging
from contextlib import contextmanager
from datetime import datetime

logger = logging.getLogger(__name__)

# 🔒 LangSmith is OPT-IN only
LS_ENABLED = (
    os.getenv("LANGSMITH_TRACING", "").lower() == "true"
    and bool(os.getenv("LANGSMITH_API_KEY"))
)

_client = None

def _get_client():
    global _client
    if not LS_ENABLED:
        return None
    if _client is None:
        try:
            from langsmith import Client
            _client = Client()
        except Exception as e:
            logger.warning("LangSmith client init failed: %s", e)
            _client = None
    return _client


@contextmanager
def ls_trace(name: str, metadata: dict | None = None):
    """
    Safe LangSmith tracing wrapper.
    - ZERO impact when disabled
    - NEVER throws
    - NEVER blocks LLM execution
    """
    run = None
    client = _get_client()

    try:
        if client:
            run = client.create_run(
                name=name,
                run_type="chain",
                inputs=metadata or {}
            )
        yield

        if client and run:
            client.update_run(
                run.id,
                end_time=datetime.utcnow(),
                outputs={"status": "success"}
            )

    except Exception as e:
        # ⛔ DO NOT RAISE
        logger.debug("LangSmith trace suppressed: %s", e)

        try:
            if client and run:
                client.update_run(
                    run.id,
                    end_time=datetime.utcnow(),
                    error=str(e),
                    outputs={"status": "failed"}
                )
        except Exception:
            pass
