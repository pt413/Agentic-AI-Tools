from sqlalchemy.orm import Session
from sqlalchemy import desc, and_, func, or_, text
import logging
from datetime import datetime, timedelta, timezone
from app.model.whatsapp_lid_mapping import WhatsAppLidMapping
from app.model.message import Message
import json
import re
from fastapi import HTTPException
from app.utils.whatsapp_utils import normalize_number
from app.services.sse_manager import sse_manager

logger = logging.getLogger(__name__)


# WhatsApp session management
async def init_client(data: dict, db: Session):
    """Initialize WhatsApp client for a number"""
    number = data.get("number")
    if not number:
        return {"error": "Number is required"}

    try:
        from app.services.whatsapp_service import start_client_for_number
        result = start_client_for_number(number)
        return result
    except Exception as err:
        return {"error": str(err)}


async def initialize_whatsapp(db: Session):
    """Initialize WhatsApp session with headless browser and return QR code"""
    try:
        from app.services.whatsapp_service import initialize_headless_session
        result = initialize_headless_session()
        return result
    except Exception as err:
        print(f"Error initializing WhatsApp: {str(err)}")
        return {"error": str(err)}

async def check_verification_status(session_id: str, db: Session):
    """Check if WhatsApp QR code has been scanned and verified"""
    try:
        from app.services.whatsapp_service import check_session_status
        result = check_session_status(session_id)
        return result
    except Exception as err:
        print(f"Error checking verification status: {str(err)}")
        return {"error": str(err)}

async def get_qr_code(session_id: str, db: Session):
    """Return the latest QR code data URL persisted by the helper for a session."""
    try:
        # Determine services directory (same dir as whatsapp_service.py)
        import os
        from app.services.whatsapp_service import __file__ as ws_file
        services_dir = os.path.dirname(ws_file)
        last_qr_path = os.path.join(services_dir, '.baileys_auth', session_id, 'last_qr.txt')
        if not os.path.exists(last_qr_path):
            return {"qrCode": None, "message": "QR not found"}
        with open(last_qr_path, 'r', encoding='utf-8') as f:
            data_url = f.read().strip()
        return {"qrCode": data_url}
    except Exception as err:
        print(f"❌ Error reading latest QR: {str(err)}")
        return {"error": str(err)}

async def save_qr_code(session_id: str, qr_code: str, db: Session):
    """Save QR code for a session"""
    try:
        import os
        from app.services.whatsapp_service import __file__ as ws_file
        services_dir = os.path.dirname(ws_file)
        auth_dir = os.path.join(services_dir, '.baileys_auth', session_id)
        os.makedirs(auth_dir, exist_ok=True)
        qr_path = os.path.join(auth_dir, 'last_qr.txt')
        
        with open(qr_path, 'w', encoding='utf-8') as f:
            f.write(qr_code)
        
        return {"message": "QR code saved successfully"}
    except Exception as err:
        print(f"❌ Error saving QR: {str(err)}")
        return {"error": str(err)}

async def get_latest_qr(session_id: str, db: Session):
    """Return the latest QR code data URL persisted by the helper for a session."""
    try:
        # Determine services directory (same dir as whatsapp_service.py)
        import os
        from app.services.whatsapp_service import __file__ as ws_file
        services_dir = os.path.dirname(ws_file)
        last_qr_path = os.path.join(services_dir, '.baileys_auth', session_id, 'last_qr.txt')
        if not os.path.exists(last_qr_path):
            return {"qrCode": None, "message": "QR not found"}
        with open(last_qr_path, 'r', encoding='utf-8') as f:
            data_url = f.read().strip()
        return {"qrCode": data_url}
    except Exception as err:
        print(f"❌ Error reading latest QR: {str(err)}")
        return {"error": str(err)}

async def check_client_status(number: str, db: Session):
    """Check client status for a number"""
    from app.services.whatsapp_service import get_client_status
    return get_client_status(number)

async def check_global_status(db: Session):
    """Get global WhatsApp session status from the database (ground truth)."""
    try:
        from app.model.wa_sessions import WhatsAppSession
        from app.services.whatsapp_service import is_pid_alive
        import os

        # Grund truth: Database sessions that are marked verified
        db_sessions = db.query(WhatsAppSession).filter(WhatsAppSession.verified == True).all()
        
        active_sessions = []
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        services_dir = os.path.join(base_dir, "services")

        for s in db_sessions:
            from app.services.whatsapp_service import is_session_really_active, is_pid_alive
            is_really_connected, reason = is_session_really_active(s.session_id)
            
            creds_path = os.path.join(services_dir, ".baileys_auth", s.session_id, "creds.json")
            has_creds = os.path.exists(creds_path)
            
            port_file = os.path.join(services_dir, ".baileys_auth", s.session_id, "bridge_port.txt")
            port_exists = os.path.exists(port_file)
            
            pid_alive = is_pid_alive(s.stream_pid) if s.stream_pid else False

            if is_really_connected:
                status = "connected"
            elif s.status == "starting":
                status = "starting"
            elif s.verified and has_creds:
                status = "authenticated"
            else:
                status = "disconnected"
            
            active_sessions.append({
                "sessionId": s.session_id,
                "phoneNumbers": [s.phone_number] if s.phone_number else [],
                "status": status,
                "pid": s.stream_pid,
                "is_active": s.is_active,
                "verified": s.verified,
                "stream_started": s.stream_started,
                "pid_alive": pid_alive,
                "port_exists": port_exists,
                "has_creds": has_creds,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None
            })
        
        return {
            "hasSession": len(active_sessions) > 0,
            "sessions": active_sessions
        }
    except Exception as err:
        print(f"Error checking global status: {str(err)}")
        return {"error": str(err)}

def utc_now():
    return datetime.now(timezone.utc)


def parse_utc_dt(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def iso_utc(value):
    if not value:
        return None

    try:
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)

        return dt.isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _heartbeat_too_old(last_heartbeat_at):
    """Return True if the last heartbeat is older than 90 seconds."""
    if not last_heartbeat_at:
        return False

    try:
        hb = last_heartbeat_at
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        else:
            hb = hb.astimezone(timezone.utc)

        return (utc_now() - hb) > timedelta(seconds=90)
    except Exception:
        return False


def _derive_dashboard_status(s, has_creds: bool, is_really_connected: bool):
    """Derive a clean display status using priority rules."""
    # 1. No credentials
    if not has_creds:
        if s.status == "pending_qr":
            return "pending_qr"
        if s.status == "needs_rescan":
            return "needs_rescan"
        return "disconnected"

    # 2. Logged out / bad auth
    if s.status in ["needs_rescan", "bad_session", "expired"]:
        return s.status

    # 3. Heartbeat stale
    if s.last_heartbeat_at and _heartbeat_too_old(s.last_heartbeat_at):
        return "unhealthy"

    # 4. Socket open
    if s.socket_state == "open" and s.socket_ready:
        return "connected"

    # 5. is_session_really_active fallback
    if is_really_connected:
        return "connected"

    # 6. Socket connecting
    if s.socket_state == "connecting":
        return "connecting"

    # 7. Socket closed
    if s.socket_state == "close":
        return "reconnecting" if s.verified else "disconnected"

    # 8. Starting
    if s.status == "starting":
        return "starting"

    # 9. Verified with creds but no socket info
    if s.verified and has_creds:
        return "authenticated"

    return s.status or "unknown"


async def get_all_sessions_list(db: Session):
    """Get all WhatsApp sessions from the database for the dashboard."""
    try:
        from app.model.wa_sessions import WhatsAppSession
        import os

        # Get all sessions, prioritizing active ones then most recent update
        from sqlalchemy import desc
        db_sessions = db.query(WhatsAppSession).order_by(
            desc(WhatsAppSession.is_active),
            desc(WhatsAppSession.updated_at)
        ).all()
        
        sessions_dict = {} # Keyed by phone_number to deduplicate
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        services_dir = os.path.join(base_dir, "services")

        # Collect phone numbers for last-saved-message lookup
        phone_numbers = []
        for s in db_sessions:
            if s.phone_number:
                phone_numbers.append(s.phone_number)

        # Query last saved message timestamp per admin_number (batch)
        last_saved_map = {}
        if phone_numbers:
            try:
                rows = db.query(
                    Message.admin_number,
                    func.max(Message.timestamp).label("last_ts")
                ).filter(
                    Message.admin_number.in_(phone_numbers)
                ).group_by(Message.admin_number).all()
                for r in rows:
                    last_saved_map[r.admin_number] = r.last_ts
            except Exception:
                pass  # non-critical

        for s in db_sessions:
            # Skip sessions without a phone number (Unknown Number)
            if not s.phone_number:
                continue

            # Determine the key for deduplication: use phone_number
            key = s.phone_number
            
            # If we've already processed this number/session, skip it (since we are sorted by updated_at desc)
            if key in sessions_dict:
                continue

            creds_path = os.path.join(services_dir, ".baileys_auth", s.session_id, "creds.json")
            has_creds = os.path.exists(creds_path)

            status = _derive_dashboard_status(s, has_creds, is_really_connected=bool(s.socket_state == "open" and s.socket_ready))

            # Filter 3: Skip old dead sessions (disconnected + creds_missing + no PID + not active)
            if (
                status == "disconnected"
                and not has_creds
                and not s.stream_pid
                and not s.is_active
            ):
                continue

            # Determine last saved message from messages table
            last_saved_ts = last_saved_map.get(s.phone_number)

            sessions_dict[key] = {
                "session_id": s.session_id,
                "phone_number": s.phone_number,
                "status": status,
                "is_active": s.is_active,
                "verified": s.verified,
                "last_connected": iso_utc(s.last_connected_at),
                "last_disconnected": iso_utc(s.last_disconnected_at),
                "updated_at": iso_utc(s.updated_at),
                "health_reason": s.last_health_reason or s.last_disconnect_reason,
                "has_creds": has_creds,

                "stream_pid": s.stream_pid,
                "stream_started": s.stream_started,
                "stream_run_id": s.stream_run_id,

                "socket_state": s.socket_state,
                "socket_ready": s.socket_ready,
                "last_health_reason": s.last_health_reason or s.last_disconnect_reason,

                "last_heartbeat_at": iso_utc(s.last_heartbeat_at),
                "last_message_upsert_at": iso_utc(s.last_message_upsert_at),
                "message_upsert_count": s.message_upsert_count,
                # keep original message timestamp as saved/source value
                "last_saved_message_at": last_saved_ts.isoformat() if last_saved_ts else None,

                "last_disconnect_code": s.last_disconnect_code,
                "last_disconnect_reason": s.last_disconnect_reason,
                "reconnect_count": s.reconnect_count,
            }
        
        return {"success": True, "sessions": list(sessions_dict.values())}
    except Exception as e:
        print(f"Error fetching all sessions: {e}")
        return {"success": False, "error": str(e)}

async def get_dashboard_summary_data(db: Session):
    try:
        sessions_res = await get_all_sessions_list(db)
        if not sessions_res.get("success"):
            return sessions_res
        
        sessions = sessions_res.get("sessions", [])
        active_numbers = [s["phone_number"] for s in sessions if s["phone_number"]]

        message_summary = {}
        if active_numbers:
            from sqlalchemy import text
            query = text("""
                SELECT DISTINCT ON (admin_number) 
                    admin_number,
                    timestamp,
                    clean_content,
                    direction
                FROM messages 
                WHERE admin_number = ANY(:admin_numbers)
                ORDER BY admin_number, timestamp DESC
            """)
            result = db.execute(query, {"admin_numbers": active_numbers}).fetchall()
            
            for row in result:
                message_summary[row[0]] = {
                    "last_message_at": row[1].isoformat() if row[1] else None,
                    "last_message_preview": row[2] or "",
                    "direction": row[3],
                }

        return {
            "success": True,
            "sessions": sessions,
            "message_summary": message_summary
        }
    except Exception as e:
        logger.error(f"Error getting dashboard summary: {e}")
        return {"success": False, "error": str(e)}


async def sync_lid_mappings(data: dict, db: Session):
    from app.model.whatsapp_group import WhatsAppGroupParticipant

    admin_number = normalize_number(data.get("admin_number"))
    mappings = data.get("mappings", {})

    if not admin_number or not mappings:
        return {"success": False, "error": "Missing admin_number or mappings"}

    learned_list = []
    for lid_jid, pn_jid in mappings.items():
        raw_lid = lid_jid
        base_lid = bareNumberFromJid(lid_jid)
        pn = normalize_number(pn_jid)
        full_lid = raw_lid if "@lid" in raw_lid else f"{raw_lid}@lid"
        
        # 1. Update/Insert into dedicated Mapping Table
        mapping_stmt = db.query(WhatsAppLidMapping).filter(WhatsAppLidMapping.lid == full_lid).first()
        if mapping_stmt:
            mapping_stmt.phone_number = pn
            mapping_stmt.admin_number = admin_number
        else:
            db.add(WhatsAppLidMapping(lid=full_lid, phone_number=pn, admin_number=admin_number))
        
        learned_list.append(full_lid)

    db.commit()

    # 2. Trigger HIGH-PERFORMANCE GLOBAL BACKFILL for these specific LIDs
    if learned_list:
        _trigger_global_backfill(db, learned_list)

    return {"success": True, "count": len(mappings)}

def _trigger_global_backfill(db: Session, lid_list: list):
    """Internal helper to run high-speed SQL backfill for a list of LIDs"""
    if not lid_list:
        return

    try:
        # We use a single SQL join-based update for maximum speed on large tables
        # Filter the mapping table for just the LIDs we just learned
        
        # 1. Update cx_number
        query_cx = text("""
            UPDATE messages
            SET cx_number = m.phone_number
            FROM whatsapp_lid_mappings m
            WHERE (messages.cx_number = m.lid OR messages.cx_number = split_part(m.lid, '@', 1))
            AND m.lid IN :lids
            AND length(messages.cx_number) > 13;
        """)
        db.execute(query_cx, {"lids": tuple(lid_list)})

        # 2. Update peer_pn
        query_peer = text("""
            UPDATE messages
            SET peer_pn = m.phone_number
            FROM whatsapp_lid_mappings m
            WHERE (messages.participant = m.lid OR messages.participant = split_part(m.lid, '@', 1))
            AND m.lid IN :lids
            AND messages.peer_pn IS NULL;
        """)
        db.execute(query_peer, {"lids": tuple(lid_list)})

        # 3. Update remote_jid
        query_jid = text("""
            UPDATE messages
            SET remote_jid = m.phone_number || '@s.whatsapp.net'
            FROM whatsapp_lid_mappings m
            WHERE (messages.remote_jid = m.lid OR messages.remote_jid = split_part(m.lid, '@', 1))
            AND m.lid IN :lids
            AND messages.remote_jid LIKE '%@lid';
        """)
        db.execute(query_jid, {"lids": tuple(lid_list)})
        
        db.commit()
        logger.info(f"✅ Global backfill completed for {len(lid_list)} LIDs")
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Error during auto-backfill: {e}")

async def sync_whatsapp_group(data: dict, db: Session):
    """Handle Group metadata from Baileys."""
    from app.model.whatsapp_group import WhatsAppGroup, WhatsAppGroupParticipant

    admin_number = normalize_number(data.get("admin_number"))
    group_jid = data.get("id")
    subject = data.get("subject")

    if not admin_number or not group_jid:
        return {"success": False, "error": "Missing admin_number or group id"}

    existing_group = db.query(WhatsAppGroup).filter(
        WhatsAppGroup.id == group_jid,
        WhatsAppGroup.admin_number == admin_number
    ).first()

    creation_ts = None
    if data.get("creation"):
        try:
            creation_ts = datetime.fromtimestamp(data.get("creation"))
        except Exception:
            creation_ts = None

    if existing_group:
        existing_group.subject = subject
        existing_group.description = data.get("desc")
        existing_group.owner = data.get("owner")
        existing_group.creation = creation_ts
        existing_group.last_sync = datetime.utcnow()
    else:
        db.add(
            WhatsAppGroup(
                id=group_jid,
                admin_number=admin_number,
                subject=subject,
                description=data.get("desc"),
                owner=data.get("owner"),
                creation=creation_ts
            )
        )

    participants = data.get("participants", [])
    if participants:
        # refresh current membership snapshot for this group/admin
        db.query(WhatsAppGroupParticipant).filter(
            WhatsAppGroupParticipant.group_id == group_jid,
            WhatsAppGroupParticipant.admin_number == admin_number
        ).delete()

        for p in participants:
            raw_id = p.get("id")          # may be PN jid OR LID jid
            raw_lid = p.get("lid")        # may be explicit LID jid
            raw_phone = p.get("phoneNumber")

            jid = None
            lid = None
            phone_number = None

            # 1) Split identity cleanly
            if raw_id:
                if "@s.whatsapp.net" in raw_id:
                    jid = raw_id
                elif "@lid" in raw_id:
                    lid = raw_id

            if raw_lid and "@lid" in raw_lid:
                lid = raw_lid

            # 2) Resolve normalized phone number
            if raw_phone:
                phone_number = normalize_number(raw_phone)
            elif jid:
                phone_number = normalize_number(jid)
            elif lid:
                # Find phone number from any existing record of this person in another group
                mapped = db.query(WhatsAppGroupParticipant).filter(
                    WhatsAppGroupParticipant.admin_number == admin_number,
                    WhatsAppGroupParticipant.lid == lid,
                    WhatsAppGroupParticipant.phone_number.isnot(None)
                ).first()
                phone_number = mapped.phone_number if mapped else None

            rank_value = p.get("rank")
            if rank_value is None and p.get("admin") is not None:
                rank_value = "admin" if p.get("admin") else None
            elif rank_value is not None:
                rank_value = str(rank_value)

            description = data.get("desc")
            if description is not None and not isinstance(description, str):
                description = json.dumps(description, default=str)

            owner = data.get("owner")
            if owner is not None:
                owner = str(owner)

            db.add(
                WhatsAppGroupParticipant(
                    group_id=group_jid,
                    admin_number=admin_number,
                    jid=jid,                    # PN jid only
                    lid=lid,                    # LID jid only
                    phone_number=phone_number,  # normalized digits only
                    rank=rank_value
                )
            )

        db.commit()

        # 3) Trigger Global Backfill if we learned any new mappings during this group sync
        # We find all participants in this group that HAVE both lid and phone_number
        learned_mappings = db.query(WhatsAppGroupParticipant.lid, WhatsAppGroupParticipant.phone_number).filter(
            WhatsAppGroupParticipant.group_id == group_jid,
            WhatsAppGroupParticipant.admin_number == admin_number,
            WhatsAppGroupParticipant.lid.isnot(None),
            WhatsAppGroupParticipant.phone_number.isnot(None)
        ).all()

        if learned_mappings:
            lid_list = []
            for l_lid, l_pn in learned_mappings:
                # Ensure it's in the global mapping table too
                full_lid = l_lid if "@lid" in l_lid else f"{l_lid}@lid"
                m_existing = db.query(WhatsAppLidMapping).filter(WhatsAppLidMapping.lid == full_lid).first()
                if not m_existing:
                    db.add(WhatsAppLidMapping(lid=full_lid, phone_number=l_pn, admin_number=admin_number))
                elif m_existing.phone_number != l_pn:
                    m_existing.phone_number = l_pn
                
                lid_list.append(full_lid)
            
            db.commit()
            if lid_list:
                _trigger_global_backfill(db, lid_list)

    return {"success": True, "id": group_jid}

async def sync_whatsapp_contact(data: dict, db: Session):
    """Handle Contact info from Baileys (can include LID -> PN mappings)."""
    from app.model.whatsapp_group import WhatsAppGroupParticipant
    from app.model.message import Message

    admin_number = normalize_number(data.get("admin_number"))
    contact_id = data.get("id")   # usually PN jid like 9198...@s.whatsapp.net
    lid = data.get("lid")         # raw lid jid like 12345@lid

    if not admin_number or not contact_id:
        return {"success": False, "error": "Missing admin_number or contact id"}

    try:
        # Resolve mapping when contact_id is a real phone jid and lid exists
        if lid and contact_id and "@s.whatsapp.net" in contact_id:
            raw_lid = lid
            base_lid = bareNumberFromJid(lid)
            norm_pn = normalize_number(contact_id)

            # 1. Update/Insert into dedicated Mapping Table (Ensure @lid suffix)
            full_lid = raw_lid if "@lid" in raw_lid else f"{raw_lid}@lid"
            mapping_stmt = db.query(WhatsAppLidMapping).filter(WhatsAppLidMapping.lid == full_lid).first()
            if mapping_stmt:
                mapping_stmt.phone_number = norm_pn
                mapping_stmt.admin_number = admin_number
            else:
                db.add(WhatsAppLidMapping(lid=full_lid, phone_number=norm_pn, admin_number=admin_number))

            # 2. Trigger backfill
            db.commit()
            _trigger_global_backfill(db, [full_lid])

        return {"success": True, "id": contact_id}

    except Exception as e:
        db.rollback()
        return {"success": False, "error": str(e), "id": contact_id}

def bareNumberFromJid(jid: str):
    if not jid: return None
    return jid.split('@')[0].split(':')[0]

async def send_message(data: dict, db: Session):
    """Send a WhatsApp message via the Baileys bridge"""
    session_id = data.get("session_id")
    admin_number = data.get("admin_number")
    jid = data.get("jid")
    text = data.get("text")
    quoted = data.get("quoted")
    mentions = data.get("mentions")
    if mentions and isinstance(mentions, str):
        mentions = [mentions]

    # lookup session_id by admin_number if missing
    if not session_id and admin_number:
        from app.services.whatsapp_service import get_session_by_number
        session_id = get_session_by_number(admin_number)

    if not session_id:
        detail = "session_id or admin_number (with active session) is required"
        if admin_number:
            detail = f"No active session found for admin_number: {admin_number}"
        raise HTTPException(status_code=404 if admin_number else 400, detail=detail)

    if not jid or not text:
        raise HTTPException(
            status_code=400,
            detail="jid and text are required"
        )

    try:
        from app.services.whatsapp_service import send_baileys_message
        result = send_baileys_message(session_id, jid, text, quoted, mentions)

        if not result.get("success", False):
            raise HTTPException(
                status_code=502,
                detail=result.get("error", "Failed to send WhatsApp message")
            )

        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def update_session_event(data: dict, db: Session):
    """Update connection status and timestamps for a WhatsApp session"""
    from app.model.wa_sessions import WhatsAppSession
    from datetime import datetime

    session_id = data.get("session_id")
    event = data.get("event")  # 'connected', 'disconnected', 'logged_out'

    if not session_id or not event:
        return {"success": False, "error": "session_id and event are required"}

    try:
        session = db.query(WhatsAppSession).filter(WhatsAppSession.session_id == session_id).first()
        if not session:
            # Create if not exists
            session = WhatsAppSession(session_id=session_id)
            db.add(session)

        now = utc_now()
        # Always update heartbeat timestamp
        session.updated_at = now

        incoming_run_id = data.get("run_id")
        if session.stream_run_id and incoming_run_id and session.stream_run_id != incoming_run_id:
            return {
                "success": False,
                "ignored": True,
                "reason": "stale_run_id",
                "current_run_id": session.stream_run_id,
                "incoming_run_id": incoming_run_id,
            }

        if incoming_run_id:
            session.stream_run_id = incoming_run_id

        if data.get("connection_state"):
            session.socket_state = data.get("connection_state")

        if data.get("statusCode") is not None:
            session.last_disconnect_code = int(data.get("statusCode") or 0)
            
        if data.get("reason"):
            session.last_disconnect_reason = str(data.get("reason"))[:2000]

        if data.get("last_socket_open_at"):
            session.last_socket_open_at = parse_utc_dt(data.get("last_socket_open_at"))

        if data.get("last_socket_close_at"):
            session.last_socket_close_at = parse_utc_dt(data.get("last_socket_close_at"))

        if data.get("last_messages_upsert_at"):
            session.last_message_upsert_at = parse_utc_dt(data.get("last_messages_upsert_at"))

        if event == 'authenticated':
            # QR scanned successfully
            session.verified = True
            
            pn = data.get("phoneNumber")
            if pn:
                session.phone_number = normalize_number(pn)
                # Consolidate: Mark other sessions for the same number as obsolete
                from sqlalchemy import and_
                other_sessions = db.query(WhatsAppSession).filter(
                    and_(
                        WhatsAppSession.phone_number == session.phone_number,
                        WhatsAppSession.session_id != session_id
                    )
                ).all()
                for old_s in other_sessions:
                    if old_s.verified or old_s.is_active:
                        print(f"🧹 Consolidating duplicate session {old_s.session_id[:8]} for number {session.phone_number}")
                        old_s.verified = False
                        old_s.is_active = False
                        old_s.status = "obsolete"
                        old_s.stream_started = False

                        # Explicitly kill the old process in background to free resources
                        import threading
                        from app.services.whatsapp_service import stop_session
                        threading.Thread(target=stop_session, args=(old_s.session_id,), daemon=True).start()

            # CRITICAL FIX: Do not downgrade an already running stream.
            if session.status == "connected" or session.is_active or session.stream_pid:
                db.commit()
                return {
                    "success": True,
                    "event": "authenticated_ignored_for_active_stream",
                    "session_id": session_id,
                }

            session.is_active = False
            session.stream_started = False
            session.stream_pid = None
            session.status = "authenticated"

        elif event == 'connected':
            pid = data.get("pid")
            if not pid:
                # QR helper or bad caller should not be allowed to mark connected
                session.verified = True
                session.is_active = False
                session.stream_started = False
                session.stream_pid = None
                session.status = "authenticated"

                pn = data.get("phoneNumber")
                if pn:
                    session.phone_number = normalize_number(pn)

                db.commit()
                return {
                    "success": True,
                    "event": "authenticated",
                    "session_id": session_id,
                    "warning": "connected_without_pid_downgraded_to_authenticated"
                }

            session.verified = True
            session.is_active = True
            session.stream_started = True
            session.stream_pid = int(pid)
            session.status = "connected"
            session.socket_state = "open"
            session.socket_ready = True
            session.last_health_reason = "socket_open"
            session.last_connected_at = now
            # Clear stale disconnect info on successful connect
            session.last_disconnect_code = None
            session.last_disconnect_reason = None

            if data.get("phoneNumber"):
                session.phone_number = normalize_number(data.get("phoneNumber"))

        elif event == 'heartbeat':
            session.verified = True
            session.is_active = True
            session.status = "connected"
            
            if data.get("pid"):
                session.stream_pid = int(data.get("pid"))
                session.stream_started = True

        elif event == 'disconnected':
            session.is_active = False
            session.stream_started = False
            session.stream_pid = None
            session.status = "disconnected"
            session.last_disconnected_at = now

        elif event == 'reconnecting':
            session.is_active = False
            # We don't clear stream_pid because the process is still alive and retrying
            session.status = "reconnecting"
            session.last_disconnected_at = now

        elif event == 'logged_out':
            print(f"🚨 [Alert] Session {session_id[:8]} was logged out (needs rescan).")
            session.is_active = False
            session.stream_started = False
            session.stream_pid = None
            session.verified = False
            session.status = "needs_rescan"
            session.last_disconnected_at = now

        db.commit()
        from app.services.event_bus import publish_event
        await publish_event("session_status", {
            "session_id": session_id,
            "event": event,
            "status": session.status,
            "socket_state": session.socket_state,
            "socket_ready": session.socket_ready,
            "last_health_reason": session.last_health_reason,
        })
        return {"success": True, "event": event, "session_id": session_id}
    except Exception as e:
        db.rollback()
        print(f"Error updating session event: {e}")
        return {"success": False, "error": str(e)}


async def session_heartbeat(session_id: str, data: dict, db: Session):
    from app.model.wa_sessions import WhatsAppSession
    from app.utils.whatsapp_utils import normalize_number

    session = db.query(WhatsAppSession).filter(
        WhatsAppSession.session_id == session_id
    ).first()

    if not session:
        return {"success": False, "error": "Session not found"}

    incoming_run_id = data.get("run_id")
    incoming_pid = data.get("pid")
    connection_state = data.get("connection_state")
    is_connected = bool(data.get("is_connected"))

    # Ignore stale events from old/killed Node process
    if session.stream_run_id and incoming_run_id and session.stream_run_id != incoming_run_id:
        return {
            "success": False,
            "ignored": True,
            "reason": "stale_run_id",
            "current_run_id": session.stream_run_id,
            "incoming_run_id": incoming_run_id,
        }

    now = utc_now()
    
    old_status = session.status
    old_socket_state = session.socket_state
    old_socket_ready = session.socket_ready
    old_reason = session.last_health_reason

    session.updated_at = now
    session.last_heartbeat_at = now

    if incoming_run_id:
        session.stream_run_id = incoming_run_id

    if incoming_pid:
        session.stream_pid = int(incoming_pid)
        session.stream_started = True

    if data.get("phone_number"):
        session.phone_number = normalize_number(data.get("phone_number"))

    session.socket_state = connection_state
    session.socket_ready = is_connected

    if data.get("last_socket_open_at"):
        session.last_socket_open_at = parse_utc_dt(data.get("last_socket_open_at"))

    if data.get("last_socket_close_at"):
        session.last_socket_close_at = parse_utc_dt(data.get("last_socket_close_at"))

    if data.get("last_messages_upsert_at"):
        session.last_message_upsert_at = parse_utc_dt(data.get("last_messages_upsert_at"))

    if data.get("message_upsert_count") is not None:
        session.message_upsert_count = int(data.get("message_upsert_count") or 0)

    if data.get("last_disconnect_code") is not None:
        session.last_disconnect_code = int(data.get("last_disconnect_code") or 0)

    if data.get("last_disconnect_reason"):
        session.last_disconnect_reason = str(data.get("last_disconnect_reason"))[:2000]

    if data.get("reconnect_count") is not None:
        session.reconnect_count = int(data.get("reconnect_count") or 0)

    # Final dashboard state
    if connection_state == "open" and is_connected:
        session.verified = True
        session.is_active = True
        session.status = "connected"
        session.last_health_reason = "socket_open"
        # Clear stale disconnect info on successful open
        session.last_disconnect_code = None
        session.last_disconnect_reason = None
        if not session.last_connected_at:
            session.last_connected_at = now

    elif connection_state == "connecting":
        session.is_active = False
        session.status = "connecting"
        session.last_health_reason = "socket_connecting"

    elif connection_state == "close":
        session.is_active = False
        session.status = "reconnecting" if session.verified else "disconnected"
        session.last_health_reason = "socket_closed"
        session.last_disconnected_at = now

    else:
        session.is_active = False
        session.status = session.status or "unknown"
        session.last_health_reason = "unknown_socket_state"

    db.commit()

    changed = (
        old_status != session.status
        or old_socket_state != session.socket_state
        or old_socket_ready != session.socket_ready
        or old_reason != session.last_health_reason
    )

    if changed:
        from app.services.event_bus import publish_event
        await publish_event("session_status", {
            "session_id": session_id,
            "event": "heartbeat",
            "status": session.status,
            "socket_state": session.socket_state,
            "socket_ready": session.socket_ready,
            "last_health_reason": session.last_health_reason,
        })

    return {
        "success": True,
        "session_id": session_id,
        "status": session.status,
        "socket_state": session.socket_state,
        "last_health_reason": session.last_health_reason,
    }

async def stop_whatsapp_session(session_id: str, db: Session):
    """Gracefully stop a WhatsApp session and all associated tracking."""
    try:
        from app.services.whatsapp_service import stop_session
        return stop_session(session_id)
    except Exception as e:
        print(f"Error stopping session {session_id}: {e}")
        return {"success": False, "error": str(e)}