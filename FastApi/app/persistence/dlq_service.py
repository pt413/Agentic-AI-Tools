import json
from typing import Any, Dict
from pathlib import Path
from app.utils.logger import get_logger

log = get_logger(__name__)


_DLQ_PATH = Path("/tmp/sync_dlq.jsonl")  

def dlq_log(reason: str, payload: Dict[str, Any]):
    """
    Record a dead-letter event to a local JSONL file and log it.
    This function is intentionally conservative: it does not create DB schema
    or change your existing tables. If you want DLQ persisted to the DB,
    we can add a migration and a DB-backed implementation later.
    """
    entry = {
        "ts": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "reason": reason,
        "payload": payload
    }
    try:
        _DLQ_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _DLQ_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as ex:
        log.exception("dlq_write_failed", exc_info=ex, extra={"reason": reason})
    log.warning("dlq_recorded", extra={"reason": reason, "payload_summary": {k: payload.get(k) for k in ("source_table", "source_id")}})
