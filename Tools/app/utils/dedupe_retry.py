"""Retry logic with exponential backoff"""
import time
from sqlalchemy.exc import OperationalError, DBAPIError
from app.dedupe_config import CONFIG
from app.utils.dedupe_logger import log

def with_retry(func, *args, max_attempts=None, operation_name="", **kwargs):
    max_attempts = max_attempts or CONFIG.RETRY_ATTEMPTS
    delay = CONFIG.RETRY_INITIAL_DELAY

    for attempt in range(max_attempts):
        try:
            log.debug(f"[RETRY] Executing {operation_name} (attempt {attempt + 1}/{max_attempts})")
            result = func(*args, **kwargs)
            if attempt > 0:
                log.info(f"[RETRY] {operation_name} succeeded after {attempt + 1} attempts")
            return result
        except (OperationalError, DBAPIError) as e:
            if attempt == max_attempts - 1:
                log.error(f"[RETRY] EXHAUSTED after {max_attempts} attempts for {operation_name}")
                raise
            log.warning(f"[RETRY] Transient error in {operation_name}, retrying in {delay:.2f}s")
            time.sleep(delay)
            delay *= CONFIG.RETRY_BACKOFF
        except Exception as e:
            log.error(f"[RETRY] Non-transient error in {operation_name}: {type(e).__name__}")
            raise
