from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
#from app.db.database import get_db
from app.model.message import Message
from app.services import whatsapp_service
from app.db.database import SessionLocal
import os

router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# -------------------------
# Ignore list management
# -------------------------
@router.get("/ignore-list")
def get_ignore_list():
    return {"ignoreList": whatsapp_service.get_ignore_list()}


@router.post("/ignore-list/add")
def add_ignore_numbers(data: dict):
    numbers = data.get("numbers", [])
    if not isinstance(numbers, list) or len(numbers) == 0:
        return {
            "message": "No numbers provided to add",
            "ignoreList": whatsapp_service.get_ignore_list()
        }
    updated_list = whatsapp_service.add_to_ignore_list(numbers)
    return {"ignoreList": updated_list}


@router.post("/ignore-list/remove")
def remove_ignore_numbers(data: dict):
    numbers = data.get("numbers", [])
    if not isinstance(numbers, list) or len(numbers) == 0:
        return {
            "message": "No numbers provided to remove",
            "ignoreList": whatsapp_service.get_ignore_list()
        }
    updated_list = whatsapp_service.remove_from_ignore_list(numbers)
    return {"ignoreList": updated_list}


# -------------------------
# WhatsApp session management
# -------------------------
@router.post("/init-client")
def init_client(data: dict):
    number = data.get("number")
    if not number:
        raise HTTPException(status_code=400, detail="Number is required")
    try:
        result = whatsapp_service.start_client_for_number(number)
        return result
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))


@router.get("/initialize-whatsapp")
def initialize_whatsapp():
    try:
        result = whatsapp_service.initialize_headless_session()
        return result
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))


@router.get("/check-verification/{session_id}")
def check_verification_status(session_id: str):
    try:
        return whatsapp_service.check_session_status(session_id)
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))


@router.get("/latest-qr/{session_id}")
def get_latest_qr(session_id: str):
    try:
        services_dir = os.path.dirname(whatsapp_service.__file__)
        last_qr_path = os.path.join(services_dir, ".baileys_auth", session_id, "last_qr.txt")
        if not os.path.exists(last_qr_path):
            raise HTTPException(status_code=404, detail="QR not found")
        with open(last_qr_path, "r", encoding="utf-8") as f:
            data_url = f.read().strip()
        return {"qrCode": data_url}
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))


@router.get("/client-status/{number}")
def check_client_status(number: str):
    return whatsapp_service.get_client_status(number)


# -------------------------
# Messages & Conversations
# -------------------------
@router.get("/messages")
def get_all_messages(
    admin_number: str = Query(..., description="Admin number is required"),
    cx_number: str = Query(None),
    from_date: str = Query(None, alias="from"),
    to_date: str = Query(None, alias="to"),
    db: Session = Depends(get_db)
):
    try:
        query = db.query(Message).filter(Message.admin_number == admin_number)

        if cx_number:
            query = query.filter(Message.cx_number == cx_number)

        # Parse ISO date filters
        if from_date:
            try:
                dt = datetime.fromisoformat(from_date)
                query = query.filter(Message.timestamp >= dt)
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid 'from' date format")
        if to_date:
            try:
                dt = datetime.fromisoformat(to_date)
                query = query.filter(Message.timestamp <= dt)
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid 'to' date format")

        query = query.filter(Message.content != "").order_by(Message.timestamp.desc())
        messages = query.all()

        results = [{
            "message_id": m.message_id,
            "direction": m.direction,
            "admin_number": m.admin_number,
            "cx_number": m.cx_number,
            "content": m.content,
            "clean_content": m.clean_content,
            "timestamp": m.timestamp.isoformat() if m.timestamp else None,
            # "media": m.media,
            "media": getattr(m, "media", None),
            "device": m.device,
            "issent": m.issent,
            "isread": m.isread
        } for m in messages]

        return {"success": True, "count": len(results), "data": results}
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))


@router.get("/conversations")
def get_conversations(
    admin_number: str = Query(...),
    group: str = Query("all"),
    db: Session = Depends(get_db)
):
    try:
        query = db.query(Message).filter(Message.admin_number == admin_number)

        now = datetime.utcnow()
        if group == "pending":
            query = query.filter(Message.isread == False)
        elif group == "lastHour":
            one_hour_ago = now - timedelta(hours=1)
            query = query.filter(Message.timestamp >= one_hour_ago)

        query = query.order_by(Message.timestamp.desc())
        messages = query.all()

        conversations = {}
        for m in messages:
            cid = m.cx_number
            if cid not in conversations:
                conversations[cid] = {
                    "contactId": cid,
                    "lastMessage": m.content,
                    "lastMessageAt": m.timestamp.isoformat() if m.timestamp else None,
                    "unreadCount": 0
                }
            if not m.isread:
                conversations[cid]["unreadCount"] += 1

        return list(conversations.values())
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))
