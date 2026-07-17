# app/routes/whatsapp_sync_route.py
from fastapi import APIRouter, Request, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.database import get_db  # assuming you already have this dependency
import httpx, os
from dotenv import load_dotenv
from datetime import datetime
from app.model.wa_msg import Msg

load_dotenv()

router = APIRouter(prefix="/whatsapp-sync", tags=["WhatsApp Sync"])

AISENSY_API_KEY = os.getenv("AISENSY_API_KEY")
AISENSY_BASE_URL = os.getenv("AISENSY_BASE_URL", "https://backend.aisensy.com/api")

# -------------------------------------------------------
# Webhook endpoint: receives incoming/outgoing messages
# -------------------------------------------------------
@router.post("/webhook")
async def whatsapp_webhook(request: Request, db: Session = Depends(get_db)):
    data = await request.json()

    try:
        message_id = data.get("message_id")
        direction = data.get("direction", "unknown")
        sender = data.get("from")
        recipient = data.get("to")
        msg_type = data.get("type", "text")
        text = data.get("text")
        status = data.get("status")
        timestamp = data.get("timestamp")

        # Convert timestamp safely
        ts = None
        if timestamp:
            try:
                ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except Exception:
                pass

        # Upsert message
        existing = db.query(Msg).filter(Msg.message_id == message_id).first()
        if not existing:
            new_msg = Msg(
                message_id=message_id,
                direction=direction,
                sender=sender,
                recipient=recipient,
                message_type=msg_type,
                text=text,
                status=status,
                timestamp=ts,
            )
            db.add(new_msg)
        else:
            if status:
                existing.status = status
            if text and not existing.text:
                existing.text = text

        db.commit()

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing webhook: {e}")

    return {"success": True}


# -------------------------------------------------------
# API endpoint to send a WhatsApp message
# -------------------------------------------------------
@router.post("/send")
async def send_message(payload: dict, db: Session = Depends(get_db)):
    to = payload.get("to")
    text = payload.get("text")

    if not to or not text:
        raise HTTPException(status_code=400, detail="Missing 'to' or 'text'")

    # Make sure there is no trailing slash
    base_url = AISENSY_BASE_URL.rstrip("/")

    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"{base_url}/sendMessage",
            headers={"Authorization": f"Bearer {AISENSY_API_KEY}"},
            json={
                "campaignName": "BP-AI-API",
                "destination": to,
                "userName": "BP_AI_BOT",
                "message": text
            }
        )

        print("status:", res.status_code)
        print("text:", res.text)

        # Handle non-JSON or failed responses gracefully
        try:
            data = res.json()
        except Exception:
            raise HTTPException(
                status_code=res.status_code,
                detail=f"Invalid response from AiSensy: {res.text}"
            )

        if res.status_code != 200:
            raise HTTPException(
                status_code=res.status_code,
                detail=f"AiSensy error: {data}"
            )

    message_id = data.get("message_id") or data.get("id") or "unknown"

    db_msg = Msg(
        message_id=message_id,
        direction="outbound",
        sender="bp_ai",
        recipient=to,
        message_type="text",
        text=text,
        status="sent"
    )
    db.add(db_msg)
    db.commit()

    return {"message_id": message_id, "sent": True, "response": data}
