"""Phase 6: Advisory locking"""
import hashlib
from sqlalchemy import text
from app.dedupe_config import CONFIG
from app.utils.dedupe_retry import with_retry
from app.utils.dedupe_logger import log

class AdvisoryLock:
    @staticmethod
    def compute_key(user_ids):
        """Compute advisory lock key"""
        sorted_ids = sorted(user_ids)
        key_str = ",".join(map(str, sorted_ids))
        hash_val = int(hashlib.sha256(key_str.encode()).hexdigest(), 16)
        key = hash_val & 0x7FFFFFFFFFFFFFFF
        log.debug(f"[LOCK] Computed key: {key} for group of {len(user_ids)}")
        return key

    @staticmethod
    def acquire(session, key, max_retries=3):
        """Acquire advisory lock"""
        def _acquire():
            log.debug(f"[LOCK] Acquiring key={key}")
            session.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
            log.info(f"[LOCK] Acquired key={key}")
            return True
        
        return with_retry(_acquire, max_attempts=max_retries, operation_name=f"advisory_lock({key})")

    @staticmethod
    def release(session, key):
        """Release advisory lock"""
        try:
            log.debug(f"[LOCK] Releasing key={key}")
            session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            log.debug(f"[LOCK] Released key={key}")
        except Exception as e:
            log.warning(f"[LOCK] Release failed: {type(e).__name__}")