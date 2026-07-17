import os
import json
import requests
import logging
from logging.handlers import RotatingFileHandler
from typing import Any
from datetime import datetime

# Configure standard logger to console
logger = logging.getLogger(__name__)

# --- WEBHOOK FILE LOGGING SETUP ---
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

LOG_FILE = os.path.join(LOG_DIR, "webhook.log")
webhook_logger = logging.getLogger("webhook_tracker")
webhook_logger.setLevel(logging.INFO)

# Avoid adding multiple handlers if the module is re-imported
if not webhook_logger.handlers:
    # 5MB per file, keep 3 backup files
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8', delay=True)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    webhook_logger.addHandler(file_handler)

def send_message_webhook(message_data: dict[str, Any]):
    """
    Send a POST request to the custom API for a WhatsApp message (incoming or outgoing).
    Triggered in the background to avoid blocking message ingestion.
    """
    webhook_url = os.getenv("WHATSAPP_WEBHOOK_URL")
    if not webhook_url:
        return

    message_id = message_data.get("message_id", "unknown")
    remote_jid = message_data.get("remote_jid", "")
    admin_number = message_data.get("admin_number")
    
    # --- 0. ADMIN WHITELIST FILTERING ---
    allowed_admins_env = os.getenv("WHATSAPP_WEBHOOK_ALLOWED_ADMINS")
    if allowed_admins_env:
        allowed_admins = [a.strip() for a in allowed_admins_env.split(",") if a.strip()]
        if admin_number not in allowed_admins:
            webhook_logger.info(f"⏭️ SKIP | {message_id} | Admin {admin_number} not in allowed whitelist.")
            return
    
    # --- 1. GROUP MENTION FILTERING ---
    if "@g.us" in remote_jid:
        raw_msg = message_data.get("raw", {})
        msg_body = raw_msg.get("message", {}) if isinstance(raw_msg, dict) else {}
        
        # Find contextInfo which contains mentionedJid
        context_info = {}
        for msg_type in ["extendedTextMessage", "imageMessage", "videoMessage", "audioMessage", "documentMessage"]:
            if msg_type in msg_body:
                context_info = msg_body[msg_type].get("contextInfo", {})
                break
        
        mentioned_jids = context_info.get("mentionedJid", [])
        
        if admin_number:
            # Step A: Resolve Admin's LID (Env -> DB)
            admin_lid = os.getenv("ADMIN_WHATSAPP_LID")
            
            if not admin_lid:
                from app.db.database import SessionLocal
                from app.model.whatsapp_group import WhatsAppGroupParticipant
                db = SessionLocal()
                try:
                    # Resolve admin's own LID from the participants table where they appear
                    p_mapping = db.query(WhatsAppGroupParticipant).filter(
                        WhatsAppGroupParticipant.phone_number == admin_number, 
                        WhatsAppGroupParticipant.lid.isnot(None)
                    ).first()
                    admin_lid = p_mapping.lid if p_mapping else None
                except Exception as db_err:
                    logger.error(f"⚠️ DB Error resolving admin LID: {db_err}")
                finally:
                    db.close()

            admin_jid = f"{admin_number}@s.whatsapp.net"
            
            # Step B: Check if Admin is mentioned
            is_mentioned = False
            if admin_lid and admin_lid in mentioned_jids:
                is_mentioned = True
            elif admin_jid in mentioned_jids:
                is_mentioned = True
            elif mentioned_jids:
                # Deep Fallback lookup
                from app.db.database import SessionLocal
                from app.model.whatsapp_group import WhatsAppGroupParticipant
                db = SessionLocal()
                try:
                    res = db.query(WhatsAppGroupParticipant).filter(
                        WhatsAppGroupParticipant.lid.in_(mentioned_jids), 
                        WhatsAppGroupParticipant.phone_number == admin_number
                    ).first()
                    if res:
                        is_mentioned = True
                except Exception as db_err:
                    logger.error(f"⚠️ DB Error in deep fallback: {db_err}")
                finally:
                    db.close()

            if not is_mentioned:
                webhook_logger.info(f"⏭️ SKIP | {message_id} | Group message but admin ({admin_number}) not mentioned. Mentions: {mentioned_jids}")
                return

    # --- 2. TRIGGER WEBHOOK ---
    try:
        participant = message_data.get("participant")
        peer_pn = message_data.get("peer_pn")

        # --- LAST MINUTE RESOLUTION ---
        # If peer_pn is missing but we have an LID participant, check the DB one last time
        if participant and not peer_pn:
            from app.db.database import SessionLocal
            from app.model.whatsapp_group import WhatsAppGroupParticipant
            db = SessionLocal()
            try:
                mapping = db.query(WhatsAppGroupParticipant).filter(
                    WhatsAppGroupParticipant.lid == participant,
                    WhatsAppGroupParticipant.phone_number.isnot(None)
                ).first()
                if mapping:
                    peer_pn = mapping.phone_number
            except Exception:
                pass
            finally:
                db.close()

        # If we have a phone number, use it for the mention JID (standard WhatsApp format)
        mention_jid = f"{peer_pn}@s.whatsapp.net" if peer_pn else participant

        is_group = remote_jid and remote_jid.endswith("@g.us")
        content = message_data.get("content") or ""

        # --- CONTENT CLEANING FOR GROUPS ---
        # Strip mention tags (@number or @lid) from the text to make it cleaner for processing
        if is_group and mentioned_jids:
            for m_jid in mentioned_jids:
                bare_id = m_jid.split('@')[0]
                content = content.replace(f"@{bare_id}", "").strip()

        payload = {
            "message_id": message_id,
            "admin_number": admin_number,
            "sender": message_data.get("cx_number"),
            "mode": "baileys",
            "content": content,
            "remote_jid": remote_jid,
            "timestamp": str(message_data.get("timestamp")) if message_data.get("timestamp") else None
        }

        # Only add group-specific sender details if it's a group message
        if is_group:
            # payload["participant"] = participant
            payload["peer_pn"] = peer_pn
            payload["mention_jid"] = mention_jid

        if message_data.get("message_type"):
            payload["raw_type"] = message_data.get("message_type")

        webhook_logger.info(f"📤 ATTEMPT | {message_id} -> {webhook_url} | Payload: {json.dumps(payload, default=str)}")
        response = requests.post(webhook_url, json=payload, timeout=10)
        
        if 200 <= response.status_code < 300:
            logger.info(f"🚀 Webhook triggered successfully: {message_id}")
            webhook_logger.info(f"✅ SUCCESS | {message_id} | Status: {response.status_code}")
        else:
            logger.warning(f"⚠️ Webhook non-200: {response.status_code} for {message_id}")
            webhook_logger.error(f"⚠️ FAILURE | {message_id} | Status: {response.status_code} | Response: {response.text[:500]}")
            
    except requests.exceptions.Timeout:
        webhook_logger.error(f"❌ TIMEOUT | {message_id} | Destination: {webhook_url}")
    except Exception as e:
        logger.error(f"❌ Webhook failed for {message_id}: {str(e)}")
        webhook_logger.error(f"❌ ERROR | {message_id} | Detail: {str(e)}")
