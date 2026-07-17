from fastapi import APIRouter, Query, HTTPException, BackgroundTasks, Depends
from typing import Optional, List, Dict, Any, Generic, TypeVar
from datetime import datetime, timedelta
import pytz
import time
import logging
from sqlalchemy.orm import Session
from sqlalchemy import Column, Integer, DateTime

# App imports
from app.generic.loader import sync_call_logs
from app.db.database import SessionLocal, Base
from app.calls.state import get_call_logs, get_call_logs_by_phone, set_call_logs
from app.calls.db_extraction import load_call_tracking_data, load_admin_summary
from app.model.call_log import CallLog
from app.model.api_sync_status import ApiSyncStatus as SyncStatus
from app.schemas.call_logs_schema import (
    MakeCallRequest, MakeCallResponse, LLMRequest, LLMSuggestionResponse,
    CallNotesRequest, CallNotesResponse, HealthResponse
)
from pydantic import BaseModel

router = APIRouter(prefix="/api", tags=["Calls"])
logging.basicConfig(level=logging.INFO)

# ----------------------------
# DB dependency
# ----------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ----------------------------
# Pagination Response
# ----------------------------
T = TypeVar("T")

class PaginatedResponse(BaseModel, Generic[T]):
    total: int
    page: int
    limit: int
    total_pages: int
    data: List[T]

    class Config:
        arbitrary_types_allowed = True

# ----------------------------
# Helpers
# ----------------------------
def serialize_doc(doc: Any):
    """Ensure logs are JSON-serializable."""
    if isinstance(doc, dict):
        return {
            k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in doc.items()
        }
    return doc

def should_sync(db: Session) -> bool:
    """Check if we should sync from API (>1hr)."""
    status = db.query(SyncStatus).order_by(SyncStatus.id.desc()).first()
    if not status:
        return True
    return datetime.utcnow() - status.last_synced > timedelta(hours=1)

def update_sync_time(db: Session):
    """Update the existing sync timestamp instead of inserting new."""
    status = db.query(SyncStatus).order_by(SyncStatus.id.desc()).first()
    if status:
        status.last_synced = datetime.utcnow()
    else:
        status = SyncStatus(last_synced=datetime.utcnow())
        db.add(status)
    db.commit()

# ----------------------------
# API Endpoints
# ----------------------------
@router.post("/sync-calls")
def sync_calls(db: Session = Depends(get_db)):
    """Force sync from API (manual trigger)."""
    try:
        sync_call_logs()
        update_sync_time(db)
        new_logs = load_call_tracking_data(db)
        set_call_logs(new_logs)
        return {"status": "ok", "message": "Forced sync complete"}
    except Exception as e:
        logging.exception(e)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/call_sync")
async def get_call_sync(db: Session = Depends(get_db)):
    """Sync once per hour, else fetch from DB/cache."""
    try:
        if should_sync(db):
            logging.info("⏳ Last sync >1hr ago. Fetching from APIs...")
            sync_call_logs()
            update_sync_time(db)  # Only update if we actually synced
            new_logs = load_call_tracking_data(db)
            set_call_logs(new_logs)
        else:
            logging.info("✅ Using cached DB data (within 1hr window)")
            # DON'T update sync time here!

        merged_data = get_call_logs(db)
        return {"status": True, "data": [serialize_doc(log) for log in merged_data]}
    except Exception as e:
        logging.exception(e)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/call_sync_admin")
async def get_call_sync_admin(db: Session = Depends(get_db)):
    try:
        summary = load_admin_summary(db)
        return {"status": True, "data": summary}
    except Exception as e:
        logging.exception(e)
        raise HTTPException(status_code=500, detail=str(e))

# ----------------------------
# Call logs with filters + pagination + deduplication
# ----------------------------
@router.get("/call-logs")
async def fetch_call_logs(
    callType: Optional[str] = Query(None),
    username: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(500, ge=1, le=1000),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    try:
        logs = get_call_logs(db)

        if callType and callType.lower() != "all":
            logs = [log for log in logs if log.get("callType", "").lower() == callType.lower()]

        if username:
            logs = [log for log in logs if username.lower() in log.get("username", "").lower()]

        if search:
            search_term = search.lower()
            logs = [
                log for log in logs
                if search_term in log.get("username", "").lower()
                or search_term in log.get("phNum", "").lower()
                or search_term in log.get("lead_id", "").lower()
                or search_term in log.get("callType", "").lower()
            ]

        logs.sort(key=lambda log: int(log.get("timestamp", 0) or 0), reverse=True)

        # Deduplicate by phNum (keep latest)
        unique_logs = list({log.get("phNum"): log for log in logs if log.get("phNum")}.values())

        # Pagination
        total = len(unique_logs)
        total_pages = (total + limit - 1) // limit if total > 0 else 1
        start_index = (page - 1) * limit
        end_index = page * limit
        paginated_logs = unique_logs[start_index:end_index]

        return {
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "data": [serialize_doc(log) for log in paginated_logs],
        }
    except Exception as e:
        logging.exception(e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/call-logs/phone/{phone_number}")
async def fetch_call_logs_by_phone(phone_number: str, db: Session = Depends(get_db)):
    try:
        logs = get_call_logs_by_phone(db, phone_number)
        return [serialize_doc(log) for log in logs]
    except Exception as e:
        logging.exception(e)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/call-logs/lead/{lead_id}")
async def fetch_call_logs_by_lead(lead_id: str, db: Session = Depends(get_db)):
    try:
        logs = [log for log in get_call_logs(db) if log.get("lead_id") == lead_id]
        if not logs:
            raise HTTPException(status_code=404, detail="No call logs found for this lead ID")
        return [serialize_doc(log) for log in logs]
    except Exception as e:
        logging.exception(e)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/call-logs/user/{username}")
async def fetch_call_logs_by_username(username: str, db: Session = Depends(get_db)):
    try:
        logs = [log for log in get_call_logs(db) if username.lower() in log.get("username", "").lower()]
        return [serialize_doc(log) for log in logs]
    except Exception as e:
        logging.exception(e)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/call-logs/user/{username}/distribution")
async def fetch_call_distribution(username: str, db: Session = Depends(get_db)):
    try:
        user_logs = [log for log in get_call_logs(db) if log.get("username", "").lower() == username.lower()]
        distribution = {}
        for log in user_logs:
            team = log.get("admin_Team", "Unknown")
            if team not in distribution:
                distribution[team] = {"count": 0, "calls": []}
            distribution[team]["count"] += 1
            distribution[team]["calls"].append({
                "adminNumber": log.get("salesPhoneNumber"),
                "adminName": log.get("username"),
                "callDate": log.get("timestamp"),
                "callType": log.get("callType"),
            })
        return distribution
    except Exception as e:
        logging.exception(e)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/call-logs-stats")
async def fetch_call_logs_stats(db: Session = Depends(get_db)):
    try:
        logs = get_call_logs(db)
        stats = {
            "totalCalls": len(logs),
            "incoming": sum(1 for log in logs if log.get("callType") == "Incoming"),
            "outgoing": sum(1 for log in logs if log.get("callType") == "Outgoing"),
            "missed": sum(1 for log in logs if log.get("callType") == "Missed"),
            "totalDuration": sum(log.get("callDuration", 0) for log in logs),
            "averageDuration": round(sum(log.get("callDuration", 0) for log in logs) / len(logs)) if logs else 0,
            "uniqueUsers": len(set(log.get("username") for log in logs if log.get("username"))),
            "uniqueNumbers": len(set(log.get("phNum") for log in logs if log.get("phNum"))),
            "leadsWithCalls": len(set(log.get("lead_id") for log in logs if log.get("lead_id"))),
            "teamsInvolved": len(set(log.get("admin_Team") for log in logs if log.get("admin_Team"))),
        }
        return stats
    except Exception as e:
        logging.exception(e)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/refresh-data")
async def refresh_data(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    def reload_data():
        try:
            new_logs = load_call_tracking_data(db)
            set_call_logs(new_logs)
            logging.info(f"[Refresh Data] Reloaded {len(new_logs)} call logs")
        except Exception as e:
            logging.error("[Refresh Data Error] " + str(e))

    background_tasks.add_task(reload_data)
    return {"success": True, "message": "Data refresh started in background."}

@router.post("/make-call")
async def make_call(data: MakeCallRequest):
    return MakeCallResponse(
        success=True,
        message="Call initiated successfully",
        callId=f"call_{int(time.time()*1000)}",
        timestamp=datetime.utcnow(),
    )

@router.post("/llm-suggestion")
async def llm_suggestion(data: LLMRequest):
    suggestions = {
        "Incoming": "Suggest following up with a product demo based on their interest.",
        "Outgoing": "Prepare to discuss pricing options and answer common objections.",
        "Missed": "Send a follow-up email and try calling again in 2 hours.",
    }
    return LLMSuggestionResponse(
        suggestion=suggestions.get(data.callType, "Review the lead's history before calling."),
        nextSteps=["Schedule follow-up", "Update CRM notes", "Send relevant materials"],
    )

@router.post("/calls/notes")
async def save_call_notes(data: CallNotesRequest):
    return CallNotesResponse(
        success=True,
        message="Notes saved successfully",
        savedAt=datetime.utcnow(),
    )

@router.get("/health")
async def health_check(db: Session = Depends(get_db)):
    return HealthResponse(
        status="OK",
        timestamp=datetime.utcnow(),
        callLogsCount=len(get_call_logs(db)),
        dataSource="PostgreSQL",
    )
