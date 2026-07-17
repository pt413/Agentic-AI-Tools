import time
import logging
from typing import List, Dict, Any
from sqlalchemy.orm import Session

# --- Global in-memory cache ---
_call_logs_cache: List[Dict[str, Any]] = []
_last_loaded: float = 0
_phone_logs_cache: Dict[str, List[Dict[str, Any]]] = {}
CACHE_TTL = 60  # seconds

def _load_from_db(db: Session) -> List[Dict[str, Any]]:
    """Fetch logs fresh from Postgres via db_extraction."""
    from app.calls.db_extraction import load_call_tracking_data
    logging.info("Loading call logs from Postgres...")
    logs = load_call_tracking_data(db)
    return logs

def get_call_logs(db: Session, force_refresh: bool = False) -> List[Dict[str, Any]]:
    """Return cached call logs, reload if expired or forced."""
    global _call_logs_cache, _last_loaded, _phone_logs_cache

    now = time.time()
    if force_refresh or not _call_logs_cache or (now - _last_loaded > CACHE_TTL):
        _call_logs_cache = _load_from_db(db)
        _last_loaded = now
        _phone_logs_cache = {}  # reset per-phone cache

    return _call_logs_cache

def get_call_logs_by_phone(db: Session, phone_number: str) -> List[Dict[str, Any]]:
    """Return cached logs for a given phone, sorted by timestamp descending."""
    global _phone_logs_cache

    if phone_number in _phone_logs_cache:
        return _phone_logs_cache[phone_number]

    logs = [log for log in get_call_logs(db) if log.get("phNum") == phone_number]
    logs.sort(key=lambda x: int(x.get("timestamp", 0)), reverse=True)

    _phone_logs_cache[phone_number] = logs
    return logs

def set_call_logs(new_logs: List[Dict[str, Any]]) -> None:
    """Manually set logs (e.g., after a forced refresh)."""
    global _call_logs_cache, _last_loaded, _phone_logs_cache
    _call_logs_cache = new_logs
    _last_loaded = time.time()
    _phone_logs_cache = {}
