import logging
from datetime import datetime
from typing import List, Dict, Any
from collections import defaultdict
import pytz

from sqlalchemy.orm import Session
from app.model.call_log import CallLog

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_call_tracking_data(db: Session) -> List[Dict[str, Any]]:
    """Fetch all call logs from Postgres."""
    try:
        logger.info("Fetching call logs from Postgres...")

        call_logs: List[CallLog] = db.query(CallLog).all()
        logger.info(f"Fetched {len(call_logs)} call log records from DB")

        logs_list: List[Dict[str, Any]] = []

        for log in call_logs:
            log_dict = {
                "username": log.username,
                "phNum": log.phNum,
                "salesPhoneNumber": log.salesPhoneNumber,
                "callType": log.callType,
                "callDuration": log.callDuration,
                "lead_id": log.lead_id,
                "timestamp": log.timestamp,
                "callDate": log.callDate.isoformat() if log.callDate else None,
                "added_on": log.added_on.isoformat() if log.added_on else None,
                "admin_Team": log.admin_Team,
            }
            logs_list.append(log_dict)

        return logs_list

    except Exception as e:
        logger.error(f"Error loading call tracking data: {e}")
        raise


def load_admin_summary(db: Session) -> List[Dict[str, Any]]:
    """Return aggregated data per admin."""
    logs = load_call_tracking_data(db)

    admin_summary: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "username": None,
        "admin_Team": None,
        "admin_phoneNumber": None,
        "totalCalls": 0,
        "lastSyncTime": None,
    })

    for log in logs:
        uname = log["username"]
        entry = admin_summary[uname]
        entry["username"] = uname
        entry["admin_Team"] = log["admin_Team"]
        entry["admin_phoneNumber"] = log.get("salesPhoneNumber")
        entry["totalCalls"] += 1

        added_on = log.get("added_on")
        if added_on:
            try:
                if isinstance(added_on, str):
                    naive_dt = datetime.fromisoformat(added_on)
                else:
                    naive_dt = added_on

                ist = pytz.timezone("Asia/Kolkata")
                ist_dt = ist.localize(naive_dt) if naive_dt.tzinfo is None else naive_dt.astimezone(ist)

                if not entry["lastSyncTime"] or ist_dt > datetime.fromisoformat(entry["lastSyncTime"]):
                    entry["lastSyncTime"] = ist_dt.isoformat()
            except Exception as e:
                logger.warning(f"Could not parse added_on time {added_on}: {e}")
                continue

    return list(admin_summary.values())
