from app.utils.logger import get_logger

logger = get_logger("unify-processors")


def process_single_call(session, af):
    logger.info("Processing call", extra={"call_id": af.id})

    # --- your logic EXACTLY as before ---
    ...
    # -------------------------------------

    logger.info("Call processed", extra={"call_id": af.id})


def process_single_message(session, m):
    logger.info("Processing message", extra={"msg_id": m.id})

    # --- your logic EXACTLY as before ---
    ...
    # -------------------------------------

    logger.info("Message processed", extra={"msg_id": m.id})


def process_single_email(session, e):
    logger.info("Processing email", extra={"email_id": e.id})

    # --- your logic EXACTLY as before ---
    ...
    # -------------------------------------

    logger.info("Email processed", extra={"email_id": e.id})
