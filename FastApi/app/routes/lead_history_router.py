from fastapi import APIRouter, Depends, HTTPException, FastAPI, Request, Body
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List
from datetime import datetime, timedelta
import requests
from app.db.database import get_db, engine, Base
from app.model.lead_history import LeadHistory
from app.schemas.lead_history_schema import LeadHistoryCreate, LeadHistoryResponse

router = APIRouter(prefix="/lead_history", tags=["Lead History"])

# -------------------- Single Entry API --------------------
@router.post("/", response_model=LeadHistoryResponse)
def create_lead_entry(entry: LeadHistoryCreate, db: Session = Depends(get_db)):
    existing = db.query(LeadHistory).filter(
        LeadHistory.lead_id == entry.lead_id,
        LeadHistory.timestamp == entry.timestamp
    ).first()

    if existing:
        return existing  # Skip duplicate

    new_entry = LeadHistory(**entry.model_dump())
    db.add(new_entry)
    db.commit()
    db.refresh(new_entry)
    return new_entry


# -------------------- Bulk Insert API --------------------
@router.post("/bulk", response_model=List[LeadHistoryResponse])
def create_bulk_lead_entries(payload: dict, db: Session = Depends(get_db)):
    data = payload.get("data", {}).get("lastVisited_details", [])
    if not data:
        raise HTTPException(status_code=400, detail="Invalid input format")

    latest_timestamp = db.query(func.max(LeadHistory.timestamp)).scalar()
    if latest_timestamp:
        data = [entry for entry in data if entry.get("timestamp") > str(latest_timestamp)]

    if not data:
        return []

    db_objs = [LeadHistory(**entry) for entry in data]
    db.add_all(db_objs)
    db.commit()
    for obj in db_objs:
        db.refresh(obj)
    return db_objs


# -------------------- Import Route (Per Lead Latest Timestamp Logic) --------------------
@router.post("/import")
async def import_from_nested_json(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
        records = body.get("data", {}).get("lastVisited_details", [])
        if not records:
            raise HTTPException(status_code=400, detail="No valid records found in request")

        existing_latest = (
            db.query(LeadHistory.lead_id, func.max(LeadHistory.timestamp))
            .group_by(LeadHistory.lead_id)
            .all()
        )
        latest_map = {lid: ts for lid, ts in existing_latest}

        inserted, skipped = 0, 0
        new_objs = []

        for r in records:
            lead_id = r.get("lead_id")
            timestamp = r.get("timestamp")
            if not lead_id or not timestamp:
                continue

            latest_ts = latest_map.get(lead_id)
            if latest_ts and timestamp <= str(latest_ts):
                skipped += 1
                continue

            obj = LeadHistory(
                referal_page=r.get("referal_page"),
                current_page=r.get("current_page"),
                ip_address=r.get("ip_address"),
                session_id=r.get("session_id"),
                user_id=r.get("user_id"),
                lead_id=lead_id,
                prop_id=r.get("prop_id"),
                timestamp=timestamp,
                source=r.get("source"),
                user_agent=r.get("user_agent"),
            )
            new_objs.append(obj)
            inserted += 1

        if new_objs:
            db.add_all(new_objs)
            db.commit()

        return {"msg": "Done", "inserted": inserted, "skipped": skipped}

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# -------------------- Auto Sync Route (Fetch from External API) --------------------
API_URL = "https://www.rentmystay.com/T/customer_browsing_history/"
AUTH_HEADERS = {
    "Authorization": "demo_token_123",   # replace with your actual token
    "Content-Type": "application/json"
}



def sync_latest_data_job(db: Session):
    """Background job: Fetch browsing history and insert only new records."""

    DEFAULT_FROM_DATE = "2025-11-01"

    latest_timestamp = db.query(func.max(LeadHistory.timestamp)).scalar()
    if latest_timestamp:
        from_date = str(latest_timestamp.date())
    else:
        from_date = DEFAULT_FROM_DATE

    try:
        response = requests.get(f"{API_URL}{from_date}", headers=AUTH_HEADERS, timeout=30)
        response.raise_for_status()
        payload = response.json()
    except Exception as e:
        print("API FETCH ERROR:", e)
        return

    data = payload.get("data", {}).get("lastVisited_details", [])
    if not data:
        print("No data from API")
        return

    if latest_timestamp:
        data = [entry for entry in data if entry.get("timestamp") > str(latest_timestamp)]

    if not data:
        print("No new records found")
        return

    db_objs = [LeadHistory(**entry) for entry in data]
    db.add_all(db_objs)
    db.commit()

    print(f"✅ AUTO SYNC COMPLETE | Inserted: {len(db_objs)}")


@router.post("/sync")
def sync_latest_data(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Fetch browsing history from RentMyStay API and insert only new records."""

    # Default date if table is empty
    DEFAULT_FROM_DATE = "2025-11-01"

    from_date = None
    if payload and payload.get("from_date"):
        from_date = payload["from_date"]
    else:
        latest_timestamp = db.query(func.max(LeadHistory.timestamp)).scalar()
        if latest_timestamp:
            from_date = str(latest_timestamp.date())
        else:
            from_date = "2025-11-01"  # 👈 used only when table has no data

    try:
        response = requests.get(f"{API_URL}{from_date}", headers=AUTH_HEADERS, timeout=30)
        response.raise_for_status()
        payload = response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch data: {e}")

    data = payload.get("data", {}).get("lastVisited_details", [])
    if not data:
        return {"msg": "No data available from API"}

    latest_timestamp = db.query(func.max(LeadHistory.timestamp)).scalar()
    if latest_timestamp:
        data = [entry for entry in data if entry.get("timestamp") > str(latest_timestamp)]
    if not data:
        return {"msg": "No newer records found"}

    db_objs = [LeadHistory(**entry) for entry in data]
    db.add_all(db_objs)
    db.commit()
    inserted = len(db_objs)

    return {
        "msg": "Sync complete",
        "inserted": inserted,
        "from_date_used": from_date
    }



# app/routes/browse_history_route.py (or same file)

def _sync_latest_data_internal(db: Session):
    """CORE LOGIC — used by API & scheduler"""

    DEFAULT_FROM_DATE = "2025-11-01"

    latest_timestamp = db.query(func.max(LeadHistory.timestamp)).scalar()
    from_date = (
        str(latest_timestamp.date())
        if latest_timestamp
        else DEFAULT_FROM_DATE
    )

    response = requests.get(
        f"{API_URL}{from_date}",
        headers=AUTH_HEADERS,
        timeout=30
    )
    response.raise_for_status()

    payload = response.json()
    data = payload.get("data", {}).get("lastVisited_details", [])

    if latest_timestamp:
        data = [
            entry for entry in data
            if entry.get("timestamp") > str(latest_timestamp)
        ]

    if not data:
        return 0

    db.add_all([LeadHistory(**entry) for entry in data])
    db.commit()
    return len(data)

# -------------------- Standalone Run Support --------------------
if __name__ == "__main__":
    import uvicorn
    app = FastAPI(title="Lead History Standalone")
    app.include_router(router)
    Base.metadata.create_all(bind=engine)
    uvicorn.run(app, host="127.0.0.1", port=8003)

    