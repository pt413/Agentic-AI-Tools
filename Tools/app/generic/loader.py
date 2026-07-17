# app/generic/loader.py
import requests
import logging
from datetime import datetime
from typing import List, Dict, Optional

from sqlalchemy.orm import Session
from app.db.database import SessionLocal
from app.model.call_log import CallLog

today_str = datetime.now().strftime("%Y-%m-%d")

CALL_LOGS_API = f"http://www.rentmystay.com/V2/get_call_logs_details/{today_str}/687ba37d3241d"
ADMIN_API = "http://www.rentmystay.com/V2/get_all_admin_details/687ba37d3241d"
BATCH_SIZE = 500

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def safe_int(value: Optional[str], default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def safe_str(value: Optional[str], default: str = "unknown") -> str:
    if value is None:
        return default
    v = str(value).strip()
    return v if v else default


def normalize_timestamp(log: Dict) -> Optional[int]:
    """Ensure timestamp is integer (ms)."""
    ts = log.get("timestamp")
    if ts:
        try:
            return int(ts)
        except Exception:
            pass
    call_date = log.get("callDate")
    if call_date:
        try:
            dt = datetime.strptime(call_date, "%Y-%m-%d %H:%M:%S")
            return int(dt.timestamp() * 1000)
        except Exception:
            logger.warning(f"Could not parse callDate: {call_date}")
    return None


def fetch_call_logs_from_api() -> List[Dict]:
    """Fetch call logs from API and return list of dicts."""
    try:
        resp = requests.get(CALL_LOGS_API, timeout=1000)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        logger.info(f"Fetched {len(data)} call logs from API")
        return data
    except Exception as e:
        logger.error(f"Error fetching call logs: {e}")
        return []


def fetch_admins_from_api() -> List[Dict]:
    """Fetch admin details from API and return list of dicts."""
    try:
        resp = requests.get(ADMIN_API, timeout=30)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        logger.info(f"Fetched {len(data)} admins from API")
        return data
    except Exception as e:
        logger.error(f"Error fetching admins: {e}")
        return []


def create_admin_mapping(admins: List[Dict]) -> Dict[str, Dict]:
    """Create a mapping of admin phone numbers to admin info."""
    admin_map = {}
    for admin in admins:
        phone = safe_str(admin.get("admin_phoneNumber"))[-10:]
        if phone:
            admin_map[phone] = {
                "admin_Team": safe_str(admin.get("admin_Team"), "Unknown"),
                "username": safe_str(admin.get("admin_uname"))
            }
    return admin_map


def insert_merged_call_logs(db: Session, logs: List[Dict], admins: List[Dict]):
    """Merge call logs with admin info and insert into Postgres."""
    inserted = 0
    skipped = 0

    admin_map = create_admin_mapping(admins)

    # Prevent duplicates using phNum, salesPhoneNumber, timestamp
    existing_keys = {
        (r.phNum, r.salesPhoneNumber, r.timestamp)
        for r in db.query(CallLog.phNum, CallLog.salesPhoneNumber, CallLog.timestamp).all()
    }

    batch = []
    for log in logs:
        ph_num = safe_str(log.get("phNum"))[-10:]
        sales_num = safe_str(log.get("salesPhoneNumber"))[-10:]
        timestamp = normalize_timestamp(log)

        if not ph_num or not sales_num or timestamp is None:
            skipped += 1
            continue

        key = (ph_num, sales_num, timestamp)
        if key in existing_keys:
            skipped += 1
            continue

        # Merge admin info from mapping
        admin_info = admin_map.get(sales_num, {})

        call_log = CallLog(
            username=admin_info.get("username", safe_str(log.get("username"))),
            phNum=ph_num,
            salesPhoneNumber=sales_num,
            callType=safe_str(log.get("callType")),
            callDuration=safe_int(log.get("callDuration")),
            lead_id=safe_str(log.get("lead_id")),
            timestamp=timestamp,
            callDate=datetime.strptime(log.get("callDate"), "%Y-%m-%d %H:%M:%S") if log.get("callDate") else None,
            added_on=datetime.strptime(log.get("added_on"), "%Y-%m-%d %H:%M:%S") if log.get("added_on") else datetime.utcnow(),
            admin_Team=admin_info.get("admin_Team", "Unknown")
        )

        batch.append(call_log)

        if len(batch) >= BATCH_SIZE:
            db.bulk_save_objects(batch)
            db.commit()
            inserted += len(batch)
            batch.clear()

    if batch:
        db.bulk_save_objects(batch)
        db.commit()
        inserted += len(batch)

    logger.info(f"Call logs inserted: {inserted}, skipped: {skipped}")


def sync_call_logs():
    """Main function to fetch from APIs, merge data, and store into sales_call_log only."""
    db = SessionLocal()
    try:
        logger.info("Fetching data from APIs...")
        logs = fetch_call_logs_from_api()
        admins = fetch_admins_from_api()

        logger.info("Merging and inserting call logs with admin info...")
        insert_merged_call_logs(db, logs, admins)

        logger.info("✅ Sync complete - Data stored in sales_call_log")
    except Exception as e:
        logger.error(f"Sync failed: {e}")
        db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    sync_call_logs()
