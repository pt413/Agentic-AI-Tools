from fastapi import APIRouter, Depends, HTTPException, Query, Request, BackgroundTasks
from fastapi.responses import StreamingResponse
import asyncio
from app.services.sse_manager import sse_manager
from sqlalchemy import desc, func, or_, case, text, and_
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime, timedelta, timezone
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv
from fastapi.responses import JSONResponse
import logging
import re
import os
import json
from app.utils.whatsapp_utils import normalize_number
from app.db.database import get_db, SessionLocal
from app.model.message import Message
from app.controllers.whatsapp_controller import (
    initialize_whatsapp,
    check_verification_status,
    check_client_status,
    check_global_status,
    get_all_sessions_list,
    get_dashboard_summary_data,
    get_qr_code,
    save_qr_code as save_qr_code_controller,
    sync_lid_mappings,
    sync_whatsapp_group,
    sync_whatsapp_contact,
    send_message,
    update_session_event,
    stop_whatsapp_session
)
from app.model.whatsapp_group import WhatsAppGroup, WhatsAppGroupParticipant
from app.model.whatsapp_lid_mapping import WhatsAppLidMapping
from app.services.webhook_service import send_message_webhook
from app.services.event_bus import publish_event

from app.services.whatsapp_ocr_service import process_whatsapp_ocr

load_dotenv()
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Add console handler if not present
if not logger.handlers:
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])


# ============================================================
# UTILITY FUNCTIONS
# ============================================================

def utc_now_naive():
    """
    Use for existing messages.last_sync column.
    It stores UTC value without timezone because messages.last_sync is DateTime without timezone.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_utc_naive(value):
    """
    Serialize naive UTC DB values with Z so frontend does not treat them as local time.
    """
    if not value:
        return None

    try:
        if value.tzinfo is None:
            return value.isoformat() + "Z"
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None

# normalize_number imported from app.utils.whatsapp_utils


def _json_safe(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def _coerce_raw_payload(msg: dict):
    raw = msg.get("raw")
    payload = raw if raw is not None else msg
    try:
        json.dumps(payload, default=str)
        return payload
    except Exception:
        return _json_safe(payload)


def process_bulk_messages(data: list, db_factory):
    """Background task to upsert messages in chunks"""
    logger.info(f"DEBUG: process_bulk_messages called with {len(data)} messages")  # Debug line
    try:
        db = db_factory()
        logger.info(f"DEBUG: Database connection established")  # Debug line
    except Exception as e:
        logger.error(f"DEBUG: Database connection failed: {e}")  # Debug line
        return
    
    try:
        CHUNK_SIZE = 1000
        metrics = {
            "total": len(data),
            "skipped": 0,
            "inserted": 0,
            "updated": 0,
            "failed": 0
        }
        affected_admins = set()
        logger.info(f"🚀 Starting bulk process: {metrics['total']} messages")
        now = utc_now_naive()

        for start in range(0, metrics["total"], CHUNK_SIZE):
            chunk = data[start:start + CHUNK_SIZE]
            rows = []

            for msg in chunk:
                try:
                    if not msg.get("admin_number"):
                        logger.warning(f"⚠️ Skipping - no admin_number")
                        metrics["skipped"] += 1
                        continue

                    admin_number = normalize_number(msg.get("admin_number"))
                    if admin_number:
                        affected_admins.add(admin_number)
                    
                    cx_number = normalize_number(msg.get("cx_number")) if msg.get("cx_number") else ""

                    message_id = msg.get("message_id")
                    if not message_id:
                        message_id = f"MSG_{now.strftime('%Y%m%d%H%M%S%f')}_{len(rows)}"

                    raw_timestamp = msg.get("timestamp")
                    if raw_timestamp:
                        if isinstance(raw_timestamp, str):
                            try:
                                timestamp = datetime.fromisoformat(raw_timestamp.replace('Z', '+00:00'))
                            except Exception:
                                timestamp = now
                        elif isinstance(raw_timestamp, datetime):
                            timestamp = raw_timestamp
                        else:
                            timestamp = now
                    else:
                        timestamp = now

                    raw_content = msg.get("content")
                    if raw_content is None:
                        msg_type = msg.get("message_type", "text")
                        raw_content = f"[Media: {msg_type.capitalize()}]" if msg_type != "text" else ""

                    clean_content = (msg.get("clean_content") or raw_content or "").replace("\n", " ").strip()

                    rows.append({
                        "message_id": message_id,
                        "admin_number": admin_number,
                        "cx_number": cx_number,
                        "content": raw_content,
                        "clean_content": clean_content,
                        "timestamp": timestamp,
                        "last_sync": now,
                        "direction": msg.get("direction", "incoming"),
                        "device": msg.get("device", "baileys"),
                        "isread": bool(msg.get("isread", False)),
                        "issent": bool(msg.get("issent", False)),
                        "message_type": msg.get("message_type", "text"),
                        "remote_jid": msg.get("remote_jid", ""),
                        "raw": _coerce_raw_payload(msg) if (msg.get("raw") or msg.get("message") or msg.get("key")) else None,
                        "r2_media_url": msg.get("r2_media_url"),
                        "participant": msg.get("participant"),
                        "peer_pn": normalize_number(msg.get("peer_pn")) if msg.get("peer_pn") else None,

                        "ocr_status": "pending" if msg.get("r2_media_url") else "no_media",
                    })
                except Exception as e:
                    logger.error(f"❌ Row preparation failed: {e}")
                    metrics["failed"] += 1
                    continue

            if not rows:
                continue

            # --- RESOLVE LIDS FOR CX_NUMBER AND PEER_PN ---
            lids_to_resolve = set()
            for r in rows:
                if r.get("cx_number") and ("@lid" in r["cx_number"] or len(r["cx_number"]) > 13): # LID-like
                    lids_to_resolve.add(r["cx_number"])
                if r.get("participant") and "@lid" in r["participant"] and not r.get("peer_pn"):
                    lids_to_resolve.add(r["participant"])

            if lids_to_resolve:
                try:
                    # Query dedicated mapping table first
                    mappings = db.query(WhatsAppLidMapping.lid, WhatsAppLidMapping.phone_number).filter(
                        or_(
                            WhatsAppLidMapping.lid.in_(lids_to_resolve),
                            # Also check bare LID if it was normalized
                            WhatsAppLidMapping.lid.in_([f"{l}@lid" for l in lids_to_resolve if "@" not in l])
                        )
                    ).all()
                    
                    lid_map = {m.lid: m.phone_number for m in mappings}
                    # Also map bare keys to the PN
                    for lid, pn in list(lid_map.items()):
                        lid_map[lid.split('@')[0]] = pn

                    for r in rows:
                        # 1. Resolve cx_number
                        cx_raw = r.get("cx_number")
                        if cx_raw in lid_map:
                            r["cx_number"] = lid_map[cx_raw]
                        
                        # 2. Resolve peer_pn
                        part_raw = r.get("participant")
                        if part_raw in lid_map and not r.get("peer_pn"):
                            r["peer_pn"] = lid_map[part_raw]
                except Exception as map_err:
                    logger.error(f"⚠️ Error resolving LIDs in bulk: {map_err}")

            # Deduplicate rows within the same chunk by message_id to prevent PostgreSQL ON CONFLICT CardinalityViolation
            unique_rows_map = {}
            for r in rows:
                unique_rows_map[r["message_id"]] = r
            
            unique_rows_list = list(unique_rows_map.values())

            # DEDUPLICATION: Check which messages already exist BEFORE we perform the upsert
            # This ensures we only trigger webhooks for TRULY new messages.
            all_ids_in_chunk = [r["message_id"] for r in unique_rows_list]
            existing_ids = set()
            try:
                # Query for IDs that are already in our database
                res = db.query(Message.message_id).filter(Message.message_id.in_(all_ids_in_chunk)).all()
                existing_ids = {r[0] for r in res}
            except Exception as e:
                logger.error(f"⚠️ Error checking existing IDs for deduplication: {e}")

            try:
                stmt = insert(Message).values(unique_rows_list)
                update_cols = {
                    "last_sync": stmt.excluded.last_sync,
                    "isread": func.coalesce(Message.isread, False) | stmt.excluded.isread,
                    # Backfill content/raw if they were missing (e.g. if a status update created the row first)
                    "content": func.coalesce(Message.content, stmt.excluded.content),
                    "clean_content": func.coalesce(Message.clean_content, stmt.excluded.clean_content),
                    "raw": func.coalesce(Message.raw, stmt.excluded.raw),
                    "r2_media_url": func.coalesce(Message.r2_media_url, stmt.excluded.r2_media_url),
                    "peer_pn": func.coalesce(Message.peer_pn, stmt.excluded.peer_pn),
                    "cx_number": case(
                        (
                            or_(
                                # 1. Existing is a LID, new is a PN (Upgrade)
                                and_(
                                    func.length(Message.cx_number) > 13,
                                    func.length(stmt.excluded.cx_number) <= 13
                                ),
                                # 2. Existing is empty/null (First real data)
                                Message.cx_number.is_(None),
                                Message.cx_number == "",
                                Message.cx_number == "0"
                            ),
                            stmt.excluded.cx_number,
                        ),
                        else_=Message.cx_number,
                    ),
                    "remote_jid": case(
                        (
                            and_(
                                Message.remote_jid.like("%@lid"),
                                stmt.excluded.remote_jid.like("%@s.whatsapp.net"),
                            ),
                            stmt.excluded.remote_jid,
                        ),
                        else_=Message.remote_jid,
                    ),

                    "ocr_status": case(
                        (
                            (Message.r2_media_url.is_(None)) &
                            (stmt.excluded.r2_media_url.isnot(None)),
                            "pending"
                        ),
                        else_=Message.ocr_status
                    )    
                }
                stmt = stmt.on_conflict_do_update(index_elements=[Message.message_id], set_=update_cols)
                db.execute(stmt)
                db.commit()
                logger.info(f"📦 Chunk {start // CHUNK_SIZE + 1}: Upserted {len(unique_rows_list)}")

                # ==========================================
                # OCR AUTO TRIGGER FOR BULK MEDIA MESSAGES
                ocr_rows = db.query(Message).filter(
                    Message.message_id.in_(all_ids_in_chunk),
                    Message.r2_media_url.isnot(None),
                    Message.ocr_status == "pending"
                ).all()

                for msg in ocr_rows:
                    try:
                        process_whatsapp_ocr(msg.id, msg.r2_media_url)
                    except Exception as e:
                        logger.error(f"❌ OCR failed for {msg.id}: {e}")
                
                # Webhook Trigger for bulk: Notify only for TRULY NEW incoming messages (deduplicated)
                for row_dict in unique_rows_list:
                    if row_dict.get("direction") == "incoming":
                        msg_id = row_dict.get("message_id")
                        # ONLY trigger if the message did NOT exist before this chunk processing
                        if msg_id not in existing_ids:
                            try:
                                send_message_webhook(row_dict)
                            except Exception as web_err:
                                logger.error(f"⚠️ Webhook error in bulk: {web_err}")
                        else:
                            # Optional: Debug log to confirm deduplication is working
                            # logger.debug(f"⏭️ Skipping webhook for existing message: {msg_id}")
                            pass
            except Exception as e:
                db.rollback()
                logger.error(f"❌ Error committing chunk: {str(e)}")

        # Log final summary
        logger.info(f"✅ Bulk Summary: Total={metrics['total']}, Skipped={metrics['skipped']}, Failed={metrics['failed']}")
        
        # Fire SSE event for bulk sync
        if affected_admins:
            from app.services.event_bus import publish_event_from_sync
            publish_event_from_sync("new_message", {
                "type": "bulk_sync",
                "admin_numbers": list(affected_admins),
            })
        
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(f"❌ Error in bulk processing: {str(e)}")
    finally:
        try:
            db.close()
        except Exception:
            pass



@router.get("/dashboard-summary")
async def get_dashboard_summary(db: Session = Depends(get_db)):
    """Lightweight snapshot of sessions and recent message summary."""
    try:
        from app.controllers.whatsapp_controller import get_dashboard_summary_data
        return await get_dashboard_summary_data(db)
    except Exception as e:
        logger.error(f"Error fetching dashboard summary: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch dashboard summary")

@router.get("/events")
async def sse_events(request: Request):
    """Server-Sent Events endpoint for real-time updates."""
    queue = sse_manager.connect()
    
    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                
                try:
                    # Wait for an event with a timeout to check for disconnects
                    message = await asyncio.wait_for(queue.get(), timeout=10.0)
                    yield message
                except asyncio.TimeoutError:
                    # Send a keep-alive comment
                    yield ": keep-alive\n\n"
                    
        except asyncio.CancelledError:
            pass
        finally:
            sse_manager.disconnect(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )





@router.get("/messages")
async def get_messages(
    admin_number: str= Query(...),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    direction: str = Query(None),
    start_date: str = Query(None),
    end_date: str = Query(None),
    db: Session = Depends(get_db)
):
    """Get messages for a specific admin with pagination and filters"""
    try:
        admin_number = normalize_number(admin_number)
        query = db.query(Message).filter(Message.admin_number == admin_number)

        if direction in ['incoming', 'outgoing']:
            query = query.filter(Message.direction == direction)

        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                query = query.filter(Message.timestamp >= start_dt)
            except ValueError:
                pass

        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                query = query.filter(Message.timestamp <= end_dt)
            except ValueError:
                pass

        # Timing the query
        import time
        t1 = time.perf_counter()
        
        # Fetch limit + 1 to determine if there is a next page without a slow count(*)
        messages_result = query.order_by(desc(Message.timestamp)).offset((page - 1) * limit).limit(limit + 1).all()
        
        t2 = time.perf_counter()
        
        has_next = len(messages_result) > limit
        if has_next:
            messages_result = messages_result[:-1] # Remove the extra record

        logger.info(f"Messages fetch for {admin_number}: DB={t2-t1:.4f}s")

        return {
            "has_next": has_next,
            "page": page,
            "limit": limit,
            "messages": [{
                "id": msg.id,
                "admin_number": msg.admin_number,
                "content": msg.content,
                "clean_content": msg.clean_content,
                # original message time, do not force UTC
                "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                # system sync time, UTC
                "last_sync": iso_utc_naive(msg.last_sync),
                "direction": msg.direction,
                "isread": msg.isread,
                "issent": msg.issent,
                "message_type": msg.message_type,
                "cx_number": msg.cx_number,
                "device": msg.device,
                "r2_media_url": msg.r2_media_url,
                "remote_jid": msg.remote_jid,
                "participant": msg.participant,
                "peer_pn": msg.peer_pn,
            } for msg in messages_result]
        }

    except Exception as e:
        logger.error(f"Error fetching messages for {admin_number}: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch messages")


@router.post("/messages")
async def add_message(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Add or update a single message with UPSERT logic"""
    try:
        data = await request.json()
        logger.info(f"📩 Single message received: {data.get('message_id', 'NO_ID')}")

        # Validate required fields
        if "admin_number" not in data or not data["admin_number"]:
            raise HTTPException(status_code=400, detail="Missing required field: admin_number")

        # Normalize numbers
        data["admin_number"] = normalize_number(data.get("admin_number"))
        if data.get("cx_number"):
            data["cx_number"] = normalize_number(data.get("cx_number"))

        # Generate message_id if missing
        if not data.get("message_id"):
            data["message_id"] = f"MSG_{utc_now_naive().strftime('%Y%m%d%H%M%S%f')}"
            logger.warning(f"⚠️ Generated message_id: {data['message_id']}")

        # Parse timestamp
        raw_timestamp = data.get("timestamp")
        if raw_timestamp:
            if isinstance(raw_timestamp, str):
                try:
                    timestamp = datetime.fromisoformat(raw_timestamp.replace('Z', '+00:00'))
                except Exception:
                    timestamp = datetime.utcnow()
            elif isinstance(raw_timestamp, datetime):
                timestamp = raw_timestamp
            else:
                timestamp = datetime.utcnow()
        else:
            timestamp = datetime.utcnow()

        # Handle content
        raw_content = data.get("content")
        if raw_content is None:
            msg_type = data.get("message_type", "text")
            raw_content = f"[Media: {msg_type.capitalize()}]" if msg_type != "text" else ""

        clean_content = (data.get("clean_content") or raw_content or "").replace("\n", " ").strip()

        # UPSERT: Check if message exists
        existing = db.query(Message).filter(Message.message_id == data["message_id"]).first()

        if existing:
            import time
            start_time = time.time()
            
            # Extract identifiers
            admin_number = data.get("admin_number", "unknown")
            message_id = data.get("message_id", "unknown")
            
            logger.info(
                f"🔍 [CHECK] Admin: {admin_number} | Msg: {message_id[:8]}..."
            )
            
            # Only update mutable status fields
            updated = False
            changes = []

            new_cx = normalize_number(data.get("cx_number")) if data.get("cx_number") else ""
            if new_cx and new_cx != existing.cx_number:
                # Upgrade logic: only allow if transitioning from LID (>13) to PN (<=13)
                # or if existing is empty/null/zero.
                if (
                    (len(existing.cx_number or "") > 13 and len(new_cx) <= 13)
                    or not existing.cx_number
                    or existing.cx_number in ["", "0"]
                ):
                    existing.cx_number = new_cx
                    updated = True
                    changes.append("cx_number")

            if data.get("content") is not None and data.get("content") != existing.content:
                existing.content = data.get("content")
                updated = True
                changes.append("content")

            if data.get("clean_content") is not None and data.get("clean_content") != existing.clean_content:
                existing.clean_content = data.get("clean_content")
                updated = True
                changes.append("clean_content")

            if timestamp and timestamp != existing.timestamp:
                existing.timestamp = timestamp
                updated = True
                changes.append("timestamp")

            if data.get("direction") and data.get("direction") != existing.direction:
                existing.direction = data.get("direction")
                updated = True
                changes.append("direction")

            if data.get("message_type") and data.get("message_type") != existing.message_type:
                existing.message_type = data.get("message_type")
                updated = True
                changes.append("message_type")

            if data.get("device") and data.get("device") != existing.device:
                existing.device = data.get("device")
                updated = True
                changes.append("device")

            # Check isread
            if data.get("isread") is not None:
                new_read = bool(data["isread"])
                if new_read and not existing.isread:
                    logger.info(f"📖 [READ] Admin: {admin_number} | False → True")
                    existing.isread = True
                    updated = True
                    changes.append("read")

            # Check issent
            if data.get("issent") is not None:
                new_sent = bool(data["issent"])
                if new_sent and not existing.issent:
                    logger.info(f"📤 [SENT] Admin: {admin_number} | False → True")
                    existing.issent = True
                    updated = True
                    changes.append("sent")

            # Preserve original raw message data
            # Only set it if it's currently None (don't overwrite with status updates)
            if existing.raw is None:
                existing.raw = data.get("raw") or data

            if data.get("r2_media_url") is not None and data.get("r2_media_url") != existing.r2_media_url:
                existing.r2_media_url = data.get("r2_media_url")
                updated = True
                changes.append("r2_media_url")

            new_remote_jid = data.get("remote_jid")
            if new_remote_jid and new_remote_jid != existing.remote_jid:
                if (
                    existing.remote_jid
                    and existing.remote_jid.endswith("@lid")
                    and new_remote_jid.endswith("@s.whatsapp.net")
                ):
                    existing.remote_jid = new_remote_jid
                    updated = True
                    changes.append("remote_jid")
                elif not existing.remote_jid:
                    existing.remote_jid = new_remote_jid
                    updated = True
                    changes.append("remote_jid")

            if data.get("participant") is not None and data.get("participant") != existing.participant:
                existing.participant = data.get("participant")
                updated = True
                changes.append("participant")

            new_peer_pn = normalize_number(data.get("peer_pn")) if data.get("peer_pn") else None
            if new_peer_pn is not None and new_peer_pn != existing.peer_pn:
                existing.peer_pn = new_peer_pn
                updated = True
                changes.append("peer_pn")

            # Save if updated
            elapsed = (time.time() - start_time) * 1000  # Convert to ms
            if updated:
                try:
                    db.commit()
                    db.refresh(existing)

                    # ================================
                    # 🔥 OCR TRIGGER FOR UPDATED MESSAGE
                    if existing.r2_media_url and existing.ocr_status != "done":
                    #if existing.r2_media_url and existing.ocr_status in ["no_media", "failed"]:    
                        existing.ocr_status = "pending"
                        db.commit()

                        background_tasks.add_task(
                            process_whatsapp_ocr,
                            existing.id,
                            existing.r2_media_url
                        )

                except IntegrityError:
                    db.rollback()
                    existing = db.query(Message).filter(Message.message_id == data["message_id"]).first()
                    return {
                        "message": "Message already exists",
                        "id": existing.id if existing else None,
                        "admin_number": admin_number,
                        "action": "duplicate",
                        "changes": changes,
                        "elapsed_ms": round(elapsed, 2)
                    }
                
                logger.info(
                    f"✅ [UPDATED] Admin: {admin_number} | "
                    f"Msg: {message_id[:8]}... | "
                    f"Fields: [{', '.join(changes)}] | "
                    f"Time: {elapsed:.2f}ms"
                )
                
                await publish_event("message_updated", {"message_id": existing.message_id, "admin_number": existing.admin_number, "changes": changes})
                
                return {
                    "message": "Message status updated",
                    "id": existing.id,
                    "admin_number": admin_number,
                    "action": "status_updated",
                    "changes": changes,
                    "elapsed_ms": round(elapsed, 2)
                }
            else:
                elapsed = (time.time() - start_time) * 1000
                
                logger.debug(
                    f"⏭️  [SKIP] Admin: {admin_number} | "
                    f"Msg: {message_id[:8]}... | "
                    f"No changes | "
                    f"Time: {elapsed:.2f}ms"
                )
                
                return {
                    "message": "Message already up-to-date",
                    "id": existing.id,
                    "admin_number": admin_number,
                    "action": "skipped",
                    "elapsed_ms": round(elapsed, 2)
                }
        else:
            # Create new
            admin_num = data.get("admin_number")
            cx = data.get("cx_number") or ""
            
            if not admin_num or not cx:
                logger.warning(f"⚠️ [SKIP] Missing admin_number ({admin_num}) or cx_number ({cx}) for msg {data.get('message_id')}")
                return {"success": False, "error": "admin_number and cx_number are required", "id": data.get("message_id")}

            cx = normalize_number(cx)

            # --- RESOLVE LIDS FOR CX_NUMBER AND PEER_PN ---
            participant = data.get("participant")
            peer_pn = normalize_number(data.get("peer_pn")) if data.get("peer_pn") else None
            
            # 1. Try resolving cx_number if it's a LID
            if cx and (len(cx) > 13 or "@lid" in cx):
                mapping = db.query(WhatsAppLidMapping).filter(
                    or_(WhatsAppLidMapping.lid == cx, WhatsAppLidMapping.lid == f"{cx}@lid")
                ).first()
                if mapping:
                    cx = mapping.phone_number

            # 2. Try resolving peer_pn if it's a LID
            if participant and "@lid" in participant and not peer_pn:
                mapping = db.query(WhatsAppLidMapping).filter(
                    WhatsAppLidMapping.lid == participant
                ).first()
                if mapping:
                    peer_pn = mapping.phone_number
                else:
                    # Fallback to group participants if not in global mapping
                    mapping = db.query(WhatsAppGroupParticipant).filter(
                        WhatsAppGroupParticipant.lid == participant,
                        WhatsAppGroupParticipant.phone_number.isnot(None)
                    ).first()
                    if mapping:
                        peer_pn = mapping.phone_number

            message = Message(
                admin_number=data["admin_number"],
                content=raw_content,
                clean_content=clean_content,
                timestamp=timestamp,
                last_sync=utc_now_naive(),
                direction=data.get("direction", "incoming"),
                device=data.get("device", "baileys"),
                isread=data.get("isread", False),
                issent=data.get("issent", False),
                message_type=data.get("message_type", "text"),
                cx_number=cx,
                remote_jid=data.get("remote_jid", ""),
                message_id=data["message_id"],
                raw=_coerce_raw_payload(data) if (data.get("raw") or data.get("message") or data.get("key")) else None,
                r2_media_url=data.get("r2_media_url"),
                participant=participant,
                peer_pn=peer_pn,
                
                ocr_status="pending" if data.get("r2_media_url") else "no_media",
            )

            db.add(message)
            try:
                db.commit()
                db.refresh(message)
                logger.info(f"✅ Created message: {data['message_id']}")
                
                await publish_event("new_message", {"message_id": message.message_id, "admin_number": message.admin_number})
                
                # ================================
                # 🔥 OCR TRIGGER (ADD THIS BLOCK)
                # ================================
                if message.r2_media_url:
                    background_tasks.add_task(
                        process_whatsapp_ocr,
                        message.id,
                        message.r2_media_url
                    )
                
                
                # Webhook Trigger: Notify only for NEW incoming messages
                if message.direction == "incoming":
                    # Convert to dict for webhook service
                    msg_dict = {
                        "message_id": message.message_id,
                        "admin_number": message.admin_number,
                        "cx_number": message.cx_number,
                        "content": message.content,
                        "remote_jid": message.remote_jid,
                        "participant": message.participant,
                        "peer_pn": message.peer_pn,
                        "timestamp": message.timestamp,
                        "message_type": message.message_type,
                        "raw": message.raw
                    }
                    background_tasks.add_task(send_message_webhook, msg_dict)
                
                return {"message": "Message added successfully", "id": message.id, "action": "created"}
            except IntegrityError:
                db.rollback()
                existing = db.query(Message).filter(Message.message_id == data["message_id"]).first()
                return {
                    "message": "Message already exists",
                    "id": existing.id if existing else None,
                    "action": "duplicate"
                }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Error adding message: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to add message: {str(e)}")


@router.post("/send")
async def send_wa_message(request: Request, db: Session = Depends(get_db)):
    """Send a WhatsApp message via the Baileys bridge"""
    try:
        data = await request.json()
        return await send_message(data, db)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending message: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))



@router.post("/messages/bulk")
async def add_bulk_messages(request: Request, background_tasks: BackgroundTasks):
    """Add or update multiple messages in bulk with background processing"""
    logger.info(f"DEBUG: Bulk endpoint called")  # Debug line
    try:
        data = await request.json()
        if not isinstance(data, list):
            raise HTTPException(status_code=400, detail="Expected array of messages")

        if len(data) == 0:
            return {"message": "No messages to process"}

        # Normalize timestamps and sort messages chronologically
        now = datetime.utcnow()
        normalized = []
        for msg in data:
            ts = msg.get("timestamp")
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    ts = now
            elif not isinstance(ts, datetime):
                ts = now

            msg["timestamp"] = ts
            msg["message_id"] = msg.get("message_id") or f"MSG_{ts.strftime('%Y%m%d%H%M%S%f')}"
            
            # Normalize numbers
            if msg.get("admin_number"):
                msg["admin_number"] = normalize_number(msg["admin_number"])
            if msg.get("cx_number"):
                msg["cx_number"] = normalize_number(msg["cx_number"])
            
            normalized.append(msg)

        # Sort messages by timestamp before inserting
        normalized.sort(key=lambda x: x["timestamp"])

        # Background upsert
        background_tasks.add_task(process_bulk_messages, normalized, SessionLocal)
        
        return {
            "message": f"Processing {len(normalized)} messages in background",
            "status": "processing"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error queueing bulk messages: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# CLIENT MANAGEMENT ENDPOINTS
# ============================================================


@router.post("/stop/{number}")
async def stop_tracking(number: str, db: Session = Depends(get_db)):
    """Stop tracking for a specific WhatsApp number"""
    try:
        from app.services.whatsapp_service import stop_tracking_number
        from fastapi.concurrency import run_in_threadpool
        
        # Run the synchronous stop logic in a threadpool to avoid blocking the event loop
        result = await run_in_threadpool(stop_tracking_number, number)
        
        if result.get("success"):
            return result
        else:
            raise HTTPException(status_code=500, detail=result.get("message", "Failed to stop tracking"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/init")
async def init_client(data: dict, db: Session = Depends(get_db)):
    """Initialize WhatsApp client for a number"""
    try:
        number = data.get("number")
        if not number:
            raise HTTPException(status_code=400, detail="Number is required")

        from app.services.whatsapp_service import start_client_for_number
        result = start_client_for_number(number)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/initialize")
async def initialize(db: Session = Depends(get_db)):
    """Initialize WhatsApp session with QR code"""
    try:
        return await initialize_whatsapp(db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reset")
async def reset_qr(payload: dict = None, db: Session = Depends(get_db)):
    """Stop an existing QR session (if provided)."""
    try:
        payload = payload or {}
        session_id = payload.get("sessionId") or payload.get("session_id")
        if not session_id:
            raise HTTPException(status_code=400, detail="sessionId is required")

        from app.services.whatsapp_service import reset_qr_session
        result = reset_qr_session(str(session_id))

        if result.get("success"):
            return result
        raise HTTPException(status_code=500, detail=result.get("message", "Failed to reset session"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# QR CODE & STATUS ENDPOINTS
# ============================================================

@router.get("/qr/{session_id}")
async def get_qr(session_id: str, db: Session = Depends(get_db)):
    """Get the latest QR code for a session"""
    try:
        return await get_qr_code(session_id, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/qr")
async def save_qr_code(data: dict, db: Session = Depends(get_db)):
    """Save QR code for a session"""
    try:
        session_id = data.get("sessionId")
        qr_code = data.get("qrCode")

        if not session_id or not qr_code:
            raise HTTPException(status_code=400, detail="sessionId and qrCode are required")

        return await save_qr_code_controller(session_id, qr_code, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/session/{session_id}")
async def check_status(session_id: str, db: Session = Depends(get_db)):
    """Check verification status of a session"""
    try:
        return await check_verification_status(session_id, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/client/{number}")
async def status(number: str, db: Session = Depends(get_db)):
    """Check client status for a number"""
    try:
        return await check_client_status(number, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
async def global_status(db: Session = Depends(get_db)):
    """Get global WhatsApp session status"""
    try:
        return await check_global_status(db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/sessions")
async def all_sessions(db: Session = Depends(get_db)):
    """Get all WhatsApp sessions for the dashboard"""
    try:
        return await get_all_sessions_list(db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


    

#=============================================================
# Extracting the data to show on dashboard UI
#=============================================================
@router.get("/show_on_UI")
async def dashboard_data(db: Session = Depends(get_db)):
    """
    Independent router for WhatsApp Dashboard UI.
    Returns formatted data matching the frontend's expected fields.
    """

    # ✅ Try to safely fetch data from WhatsappDetails table
    try:
        # ✅ Aggregate data by admin_number (WhatsApp account)
        excluded_numbers = ["15551514853", "917411342997","9174110 74215",]

        raw_data = (
        db.query(
        Message.admin_number.label("whatsapp_account"),
        func.count(Message.cx_number.distinct()).label("Total_customers"),
        func.min(Message.timestamp).label("From_date"),
        func.max(Message.timestamp).label("To_date"),
        func.max(Message.last_sync).label("Last_sync"),
    )
    .filter(~Message.admin_number.in_(excluded_numbers))   # ✅ exclude here
    .group_by(Message.admin_number)
    .all()
)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching WhatsApp data: {e}")

    if not raw_data:
        raise HTTPException(status_code=404, detail="No WhatsApp data found")

    result = []
    now = datetime.utcnow()

    for r in raw_data:
        result.append({
            "whatsapp_account": str(r.whatsapp_account or "—"),
            "Total_customers": int(r.Total_customers or 0),
            "From_date": r.From_date.isoformat() if r.From_date else None,
            "To_date": r.To_date.isoformat() if r.To_date else None,
            "Last_sync": r.Last_sync.isoformat() if r.Last_sync else None,
        })

    return result





@router.get("/lid-mappings/{lid}")
async def get_lid_mapping(lid: str, db: Session = Depends(get_db)):
    """Get a phone number mapping for a LID"""
    try:
        # Check for both bare and JID formats
        mapping = db.query(WhatsAppLidMapping).filter(
            or_(WhatsAppLidMapping.lid == lid, WhatsAppLidMapping.lid == f"{lid}@lid")
        ).first()
        
        if mapping:
            return {"lid": mapping.lid, "phone_number": mapping.phone_number}
        return {"lid": lid, "phone_number": None}
    except Exception as e:
        logger.error(f"Error fetching LID mapping: {e}")
        return {"error": str(e)}

@router.post("/lid-mappings")
async def add_lid_mappings(data: dict, db: Session = Depends(get_db)):
    """Add or update LID to Phone Number mappings"""
    try:
        return await sync_lid_mappings(data, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/group")
async def add_whatsapp_group(data: dict, db: Session = Depends(get_db)):
    try:
        return await sync_whatsapp_group(data, db)
    except Exception as e:
        logger.exception(
            "whatsapp/group failed",
            extra={
                "group_id": data.get("id"),
                "admin_number": data.get("admin_number"),
                "subject": data.get("subject"),
                "participant_count": len(data.get("participants", []) or []),
            },
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/contact")
async def add_whatsapp_contact(data: dict, db: Session = Depends(get_db)):
    """Add or update WhatsApp Contact info"""
    try:
        return await sync_whatsapp_contact(data, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/session/event")
async def session_event(data: dict, db: Session = Depends(get_db)):
    """Handle session connection/disconnection events from Baileys"""
    try:
        return await update_session_event(data, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/session/{session_id}/heartbeat")
async def session_heartbeat_route(session_id: str, request: Request, db: Session = Depends(get_db)):
    """Keep session alive and update real Baileys socket health."""
    try:
        from app.controllers.whatsapp_controller import session_heartbeat
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        return await session_heartbeat(session_id, payload, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/stop/{number}")
async def stop_by_number(number: str, db: Session = Depends(get_db)):
    """Stop a WhatsApp session by phone number"""
    try:
        from app.services.whatsapp_service import number_to_session
        session_id = number_to_session.get(number)
        if not session_id:
            # Try to find in DB if not in memory
            from app.model.wa_sessions import WhatsAppSession
            session_db = db.query(WhatsAppSession).filter(WhatsAppSession.phone_number == number).first()
            if session_db:
                session_id = session_db.session_id
        
        if not session_id:
            return {"success": False, "error": f"No active session found for number {number}"}
            
        return await stop_whatsapp_session(session_id, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/session/stop/{session_id}")
async def session_stop(session_id: str, db: Session = Depends(get_db)):
    """Gracefully stop a WhatsApp session by ID"""
    try:
        return await stop_whatsapp_session(session_id, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))












# ============================================================
# PAYMENT METADATA ENDPOINT
# ============================================================
@router.get("/payment-metadata/by-phone")
async def get_payment_metadata_by_phone(
    phone: str = Query(..., description="Customer phone number"),
    admin_number: Optional[str] = Query(None, description="Optional WhatsApp account/admin number"),
    limit: int = Query(100, ge=1, le=500),
    include_text: bool = Query(False, description="Set true to include extracted OCR text"),
    db: Session = Depends(get_db),
):
    try:
        def only_digits(value):
            return re.sub(r"\D", "", str(value or ""))

        normalized_phone = normalize_number(phone) or phone
        phone_digits = only_digits(normalized_phone)

        if not phone_digits:
            raise HTTPException(status_code=400, detail="Invalid phone number")

        phone_last10 = phone_digits[-10:] if len(phone_digits) >= 10 else phone_digits

        normalized_admin = normalize_number(admin_number) if admin_number else None
        admin_digits = only_digits(normalized_admin) if normalized_admin else None
        admin_last10 = admin_digits[-10:] if admin_digits and len(admin_digits) >= 10 else admin_digits

        sql = """
            WITH payment_rows AS (
                SELECT
                    id,
                    message_id,
                    admin_number,
                    cx_number,
                    peer_pn,
                    participant,
                    remote_jid,
                    r2_media_url,
                    ocr_status,
                    extracted_text,
                    image_type,
                    payment_metadata,
                    timestamp,
                    regexp_replace(coalesce(peer_pn, ''), '[^0-9]', '', 'g') AS peer_digits,
                    regexp_replace(coalesce(cx_number, ''), '[^0-9]', '', 'g') AS cx_digits,
                    regexp_replace(coalesce(admin_number, ''), '[^0-9]', '', 'g') AS admin_digits
                FROM public.messages
                WHERE image_type = 'payment_receipt'
                  AND payment_metadata IS NOT NULL
                  AND payment_metadata <> '{}'::jsonb
            )
            SELECT
                id,
                message_id,
                admin_number,
                cx_number,
                peer_pn,
                participant,
                remote_jid,
                r2_media_url,
                ocr_status,
                extracted_text,
                image_type,
                payment_metadata,
                timestamp,
                CASE
                    WHEN peer_digits = :phone_digits
                      OR right(peer_digits, 10) = :phone_last10
                    THEN 'peer_pn'

                    WHEN (
                        peer_pn IS NULL
                        OR peer_pn = ''
                        OR peer_digits = ''
                    )
                    AND (
                        cx_digits = :phone_digits
                        OR right(cx_digits, 10) = :phone_last10
                    )
                    THEN 'cx_number'

                    ELSE 'unknown'
                END AS matched_by,
                CASE
                    WHEN peer_pn IS NOT NULL AND peer_pn <> ''
                    THEN peer_pn
                    ELSE cx_number
                END AS resolved_customer_number
            FROM payment_rows
            WHERE
                (
                    peer_digits = :phone_digits
                    OR right(peer_digits, 10) = :phone_last10

                    OR

                    (
                        (
                            peer_pn IS NULL
                            OR peer_pn = ''
                            OR peer_digits = ''
                        )
                        AND (
                            cx_digits = :phone_digits
                            OR right(cx_digits, 10) = :phone_last10
                        )
                        AND coalesce(cx_number, '') NOT ILIKE '%@g.us%'
                        AND coalesce(cx_number, '') NOT ILIKE '%g.us%'
                        AND coalesce(cx_number, '') NOT LIKE '%-%'
                        AND length(cx_digits) BETWEEN 10 AND 13
                    )
                )
        """

        params = {
            "phone_digits": phone_digits,
            "phone_last10": phone_last10,
            "limit": limit,
        }

        if admin_digits:
            sql += """
                AND (
                    admin_digits = :admin_digits
                    OR right(admin_digits, 10) = :admin_last10
                )
            """
            params["admin_digits"] = admin_digits
            params["admin_last10"] = admin_last10

        sql += """
            ORDER BY timestamp DESC NULLS LAST, id DESC
            LIMIT :limit
        """

        rows = db.execute(text(sql), params).fetchall()

        data = []
        total_amount = 0.0
        amount_count = 0

        for row in rows:
            metadata = row.payment_metadata or {}

            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except Exception:
                    metadata = {}

            amount = metadata.get("amount")
            if amount is not None:
                try:
                    total_amount += float(amount)
                    amount_count += 1
                except Exception:
                    pass

            item = {
                "admin_number": row.admin_number,
                "customer_number": row.resolved_customer_number,
                "matched_by": row.matched_by,
                "cx_number": row.cx_number,
                "peer_pn": row.peer_pn,
                "payment_metadata": metadata,
            }

            if include_text:
                item["extracted_text"] = row.extracted_text

            data.append(item)

        return {
            "success": True,
            "phone": phone,
            "normalized_phone": normalized_phone,
            "matched_last10": phone_last10,
            "count": len(data),
            "amount_count": amount_count,
            "total_amount": total_amount,
            "data": data,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching payment metadata for phone={phone}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch payment metadata")
