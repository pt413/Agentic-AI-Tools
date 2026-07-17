from app.utils.logger import get_logger
from app.utils.error_handler import safe_execute
from app.db.database import SessionLocal

from .extractors import fetch_calls, fetch_messages, fetch_emails
from .processors import process_single_call, process_single_message, process_single_email

logger = get_logger("unify-activity-sync")


def _process_with_session(processor_func, source_obj, context):
    """Short-lived DB session wrapper."""
    session = SessionLocal()
    try:
        return processor_func(session, source_obj)
    finally:
        try:
            session.close()
        except Exception:
            logger.exception("Error closing session", extra=context)


def sync_all(batch_size: int = 100):
    logger.info("Starting unify-activity sync job")

    # --- Calls ---
    for rows in fetch_calls(batch_size=batch_size):
        for call in rows:
            context = {"type": "call", "id": getattr(call, "id", None)}
            safe_execute(
                lambda: _process_with_session(process_single_call, call, context),
                logger, context
            )

    # --- Messages ---
    for rows in fetch_messages(batch_size=batch_size):
        for msg in rows:
            context = {"type": "message", "id": getattr(msg, "id", None)}
            safe_execute(
                lambda: _process_with_session(process_single_message, msg, context),
                logger, context
            )

    # --- Emails ---
    for rows in fetch_emails(batch_size=batch_size):
        for email in rows:
            context = {"type": "email", "id": getattr(email, "id", None)}
            safe_execute(
                lambda: _process_with_session(process_single_email, email, context),
                logger, context
            )

    logger.info("Completed unify-activity sync job")
