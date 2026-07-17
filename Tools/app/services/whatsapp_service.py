# whatsapp_service.py
import os
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.db.database import SessionLocal
import subprocess, json, requests
from datetime import datetime
import re
import threading
import time
import logging
import traceback
from app.utils.whatsapp_utils import normalize_number, normalize_jid

logger = logging.getLogger(__name__)

BRIDGE_HOST = os.getenv("BRIDGE_HOST", "127.0.0.1")

# Global lock for creating per-session locks
global_lock = threading.Lock()
session_locks = {}

def get_session_lock(session_id):
    """Retrieve or create a lock for a specific session"""
    with global_lock:
        if session_id not in session_locks:
            session_locks[session_id] = threading.Lock()
        return session_locks[session_id]

def is_pid_alive(pid: int):
    """Check if a process PID is currently running on the OS."""
    if not pid: return False
    import os
    if os.name == 'nt':
        # Windows
        import subprocess
        try:
            # tasklist filter for specific PID
            output = subprocess.check_output(["tasklist", "/FI", f"PID eq {pid}", "/NH"], text=True)
            return str(pid) in output
        except Exception:
            return False
    else:
        # Unix
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

def get_bridge_health(session_id: str):
    """Call the Node.js bridge /health endpoint to get its internal status."""
    from app.db.database import SessionLocal
    from app.model.wa_sessions import WhatsAppSession
    
    db = SessionLocal()
    try:
        s = db.query(WhatsAppSession).filter(WhatsAppSession.session_id == session_id).first()
        if not s: return None
        
        base_dir = os.path.dirname(os.path.abspath(__file__))
        port_file = os.path.join(base_dir, ".baileys_auth", session_id, "bridge_port.txt")
        
        if not os.path.exists(port_file):
            return None
            
        with open(port_file, "r", encoding="utf-8") as f:
            port = f.read().strip()
            
        if not port: return None
        
        r = requests.get(f"http://{BRIDGE_HOST}:{port}/health", timeout=2)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    finally:
        db.close()
    return None

def is_session_really_active(session_id: str):
    """
    Ultimate source of truth for an active WhatsApp session:
    1) creds.json exists
    2) bridge PID is alive
    3) bridge_port.txt exists
    4) bridge /health says is_connected = True
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    creds_path = os.path.join(base_dir, ".baileys_auth", session_id, "creds.json")
    port_file = os.path.join(base_dir, ".baileys_auth", session_id, "bridge_port.txt")
    
    # 1. Creds check
    if not os.path.exists(creds_path):
        return False, "creds_missing"
        
    # 2. PID check
    from app.db.database import SessionLocal
    from app.model.wa_sessions import WhatsAppSession
    db = SessionLocal()
    s = db.query(WhatsAppSession).filter(WhatsAppSession.session_id == session_id).first()
    db.close()
    
    if not s or not s.stream_pid or not is_pid_alive(s.stream_pid):
        return False, "pid_dead"
        
    # 3. Port check
    if not os.path.exists(port_file):
        return False, "port_missing"
        
    # 4. Bridge /health check
    health = get_bridge_health(session_id)
    if not health:
        return False, "bridge_unreachable"
    
    if not health.get("is_connected"):
        return False, "whatsapp_disconnected"
        
    return True, "active"

def cleanup_stale_sessions():
    """Periodically check for sessions that missed heartbeats or died silently.
    If a session is verified but inactive/ghost, attempt restoration.
    """
    from app.db.database import SessionLocal
    from app.model.wa_sessions import WhatsAppSession
    from datetime import datetime, timedelta
    
    db = SessionLocal()
    try:
        # 1. Handle Stale Heartbeats (missed for > 3 mins)
        stale_threshold = datetime.utcnow() - timedelta(seconds=180)
        stale_sessions = db.query(WhatsAppSession).filter(
            WhatsAppSession.is_active == True,
            WhatsAppSession.updated_at < stale_threshold
        ).all()
        
        for s in stale_sessions:
            print(f"⚠️ [Supervisor] Session {s.session_id[:8]} missed heartbeat. Verifying health...")
            is_active, reason = is_session_really_active(s.session_id)
            
            if is_active:
                # PID is alive and bridge is connected, maybe just a network glitch?
                # Update heartbeat to prevent repeated checks
                s.updated_at = datetime.utcnow()
                db.commit()
                continue

            # If it's NOT really active (e.g. bridge disconnected or PID dead)
            pid_alive = is_pid_alive(s.stream_pid) if s.stream_pid else False
            print(f"🧹 [Supervisor] Cleaning stale/unhealthy session {s.session_id[:8]} (Reason: {reason}, PID Alive: {pid_alive})")
            
            s.is_active = False
            s.status = "disconnected"
            
            # CRITICAL: Only clear PID if it's actually dead. 
            # If it's alive but unhealthy, we KEEP the PID so check_session_status can kill it.
            if not pid_alive:
                s.stream_pid = None
                s.stream_started = False
            
            db.commit()
            
            # Clean up memory state
            if s.session_id in clients:
                del clients[s.session_id]

        # 2. Handle Ghost Sessions (Verified but not active)
        # We try to restore verified sessions that are marked as inactive
        ghost_sessions = db.query(WhatsAppSession).filter(
            WhatsAppSession.verified == True,
            WhatsAppSession.is_active == False
        ).all()
        
        for s in ghost_sessions:
            # Avoid infinite restart loops - check if we already tried recently
            # If it failed to start 5 times and was updated in the last 10 mins, maybe it's unrecoverable (e.g. banned)
            # For now, just a simple restoration attempt if it hasn't been updated in a while or status is 'disconnected'
            
            print(f"🚀 [Supervisor] Detected ghost session {s.session_id[:8]}. Attempting restoration...")
            try:
                # check_session_status handles the actual restart
                res = check_session_status(s.session_id)
                if not res.get("is_active"):
                    # If it's still not active after restoration attempt, maybe creds are bad
                    base_dir = os.path.dirname(os.path.abspath(__file__))
                    creds_path = os.path.join(base_dir, ".baileys_auth", s.session_id, "creds.json")
                    if not os.path.exists(creds_path):
                        print(f"❌ [Supervisor] restoration failed for {s.session_id[:8]} - creds missing. Marking unverified.")
                        s.verified = False
                        s.status = "disconnected"
                        db.commit()
            except Exception as e:
                print(f"❌ [Supervisor] restoration failed for {s.session_id[:8]}: {e}")
                
    except Exception as e:
        print(f"❌ Error during supervisor cleanup: {e}")
    finally:
        db.close()


def restore_active_whatsapp_sessions():
    """Startup task: Restore and restart bridges for all authenticated sessions.
    Source of Truth: Physical presence of creds.json in the session folder.
    """
    from app.db.database import SessionLocal
    from app.model.wa_sessions import WhatsAppSession
    import os

    db = SessionLocal()
    try:
        from sqlalchemy import or_
        # Only check sessions that are either verified, active, or have a phone number
        # This filters out 'junk' sessions that were created but never scanned.
        sessions_to_check = db.query(WhatsAppSession).filter(
            or_(
                WhatsAppSession.verified == True,
                WhatsAppSession.is_active == True,
                WhatsAppSession.phone_number.isnot(None)
            )
        ).all()
        print(f"🔄 Checking {len(sessions_to_check)} meaningful sessions for authentication files...")
        
        base_dir = os.path.dirname(os.path.abspath(__file__))
        restored_count = 0

        for s in sessions_to_check:
            baileys_creds = os.path.join(base_dir, ".baileys_auth", s.session_id, "creds.json")
            
            if os.path.exists(baileys_creds):
                # If creds exist, this session IS verified.
                if not s.verified:
                    print(f"✅ Found missing creds.json for session {s.session_id[:8]}. Restoring verified status.")
                    s.verified = True
                    db.commit()

                # Use check_session_status to handle the process restart logic
                try:
                    print(f"🚀 Restoring session {s.session_id[:8]}...")
                    res = check_session_status(s.session_id)
                    # Sync DB state after check
                    s.is_active = bool(res.get("is_active", False))

                    if res.get("status"):
                        s.status = res["status"]
                    elif s.is_active:
                        s.status = "connected"
                    else:
                        s.status = "starting" if res.get("verified") else "disconnected"

                    db.commit()
                    restored_count += 1
                except Exception as e:
                    print(f"❌ Failed to restore session {s.session_id[:8]}: {e}")
            else:
                # If no creds exist, it cannot be verified or active
                if s.verified or s.is_active:
                    print(f"🧹 Session {s.session_id[:8]} has no creds.json. Marking unverified.")
                    s.verified = False
                    s.is_active = False
                    s.status = "disconnected"
                    db.commit()
        
        print(f"✅ Restoration complete. {restored_count} sessions processed.")
                
    except Exception as e:
        print(f"❌ Error during session restoration: {e}")
    finally:
        db.close()

# Store clients with support for multiple users per session
clients = {}
tracked_numbers = set()
ignore_list = set()
session_to_numbers = {}  # Maps session_id to set of phone numbers
number_to_session = {}   # Maps phone numbers to their session_id

def resolve_active_session_for_admin(admin_number: str):
    """
    Return a sendable session_id for this admin_number, or None.
    Truth = DB row + creds.json + bridge health.
    """
    from app.db.database import SessionLocal
    from app.model.wa_sessions import WhatsAppSession
    import os
    import requests

    norm = normalize_number(admin_number)
    if not norm:
        return None

    db = SessionLocal()
    try:
        # Find the most recently updated verified session for this number
        row = (
            db.query(WhatsAppSession)
            .filter(WhatsAppSession.phone_number == norm)
            .filter(WhatsAppSession.verified == True)
            .order_by(WhatsAppSession.updated_at.desc())
            .first()
        )
        if not row:
            return None

        session_id = row.session_id
        base_dir = os.path.dirname(os.path.abspath(__file__))
        session_dir = os.path.join(base_dir, ".baileys_auth", session_id)
        creds_path = os.path.join(session_dir, "creds.json")
        port_file = os.path.join(session_dir, "bridge_port.txt")

        # 1) Authentication Check
        if not os.path.exists(creds_path):
            print(f"🚨 [Resolution] Session {session_id[:8]} missing creds.json. Marking unverified.")
            row.verified = False
            row.is_active = False
            row.stream_started = False
            row.stream_pid = None
            row.status = "disconnected"
            db.commit()
            return None

        # 2) Real-time Liveness Check
        is_alive = False
        if row.stream_pid and is_pid_alive(row.stream_pid) and os.path.exists(port_file):
            try:
                with open(port_file, "r", encoding="utf-8") as f:
                    port = f.read().strip()
                if port:
                    # Final truth: bridge health
                    r = requests.get(f"http://{BRIDGE_HOST}:{port}/health", timeout=5)
                    payload = r.json() if r.status_code == 200 else {}
                    if r.status_code == 200 and payload.get("is_connected") is True:
                        is_alive = True
            except Exception:
                pass

        if is_alive:
            # Sync DB state to active if it wasn't
            if not row.is_active or row.status != "connected":
                row.is_active = True
                row.status = "connected"
                db.commit()
            
            # Sync memory cache
            register_number_to_session(session_id, norm)
            return session_id

        # 3) Ghost Session Cleanup / Restoration Attempt
        print(f"⚠️ [Resolution] Session {session_id[:8]} found in DB but bridge is dead. Attempting restoration...")
        
        # Reset DB state before attempt
        row.is_active = False
        row.stream_started = False
        row.stream_pid = None
        row.status = "disconnected"
        db.commit()

        try:
            # check_session_status handles the actual node bridge startup
            check_session_status(session_id)
            
            # Re-fetch and check health again
            db.refresh(row)
            if row.stream_pid and is_pid_alive(row.stream_pid):
                # Small wait for bridge to initialize its internal socket
                import time
                time.sleep(2)
                
                if os.path.exists(port_file):
                    with open(port_file, "r", encoding="utf-8") as f:
                        port = f.read().strip()
                    if port:
                        r = requests.get(f"http://{BRIDGE_HOST}:{port}/health", timeout=3)
                        if r.status_code == 200 and r.json().get("is_connected") is True:
                            register_number_to_session(session_id, norm)
                            return session_id
        except Exception as e:
            print(f"❌ [Resolution] restoration failed for {session_id[:8]}: {e}")

        return None
    finally:
        db.close()

def get_session_by_number(admin_number: str):
    """
    Resolve an admin phone number to its active session_id.
    Uses memory cache for speed, fails over to DB+Health for reliability.
    """
    norm_num = normalize_number(admin_number)
    if not norm_num:
        return None
    
    # Path A: Memory Cache (Fast)
    if norm_num in number_to_session:
        # Quick validation: is the session still in our global client map?
        sid = number_to_session[norm_num]
        if sid in clients:
            return sid
    
    # Path B: DB + Health Resolution (Reliable)
    return resolve_active_session_for_admin(norm_num)

def get_db():
    return SessionLocal()

# normalize_number imported from app.utils.whatsapp_utils

def log_message(number, message, db: Session = None):
    """
    Python equivalent of the JS logMessage function
    message: WhatsAPIDriver message object
    """
    try:
        # Handle both message objects and plain data
        if hasattr(message, 'chat'):
            chat = message.chat
            from_number = message.sender.id.split("@")[0].split("-")[0]
            to_number = (
                getattr(message, "to", None)
                or getattr(chat, "id", None) or ""
            ).split("@")[0]
        else:
            # Handle plain data object (from HTTP API)
            from_number = message.get("cx_number", "")
            to_number = message.get("admin_number", "")

        if from_number not in tracked_numbers and to_number not in tracked_numbers:
            return

        # Check ignore list
        other_party = to_number if from_number == number else from_number
        ignore_flag = 1 if ignore_list and other_party in ignore_list else 0

        # Create message record for SQLAlchemy
        from app.model.message import Message

        msg_id = str(message.get("message_id", "") or "").strip()
        raw_ts = message.get("timestamp")

        if isinstance(raw_ts, str):
            try:
                msg_ts = datetime.fromisoformat(raw_ts.replace('Z', '+00:00'))
            except Exception:
                msg_ts = datetime.utcnow()
        elif isinstance(raw_ts, datetime):
            msg_ts = raw_ts
        else:
            msg_ts = datetime.utcnow()

        # Add to database
        if db:
            existing = None
            if msg_id:
                existing = db.query(Message).filter(Message.message_id == msg_id).first()

            if existing:
                # Update existing row (idempotent)
                existing.direction = message.get("direction", existing.direction)
                if "admin_number" in message:
                    existing.admin_number = normalize_number(message.get("admin_number"))
                if "cx_number" in message:
                    new_cx = normalize_number(message.get("cx_number"))
                    if new_cx and new_cx != existing.cx_number:
                        if (
                            (len(existing.cx_number or "") > 13 and len(new_cx) <= 13)
                            or not existing.cx_number
                            or existing.cx_number in ["", "0"]
                        ):
                            existing.cx_number = new_cx
                existing.content = message.get("content", existing.content)
                existing.clean_content = message.get("clean_content", existing.clean_content)
                existing.timestamp = msg_ts
                # existing.last_sync = datetime.datetime.utcnow()
                existing.device = message.get("device", existing.device)
                existing.issent = message.get("issent", existing.issent)
                existing.isread = message.get("isread", existing.isread)
                new_remote_jid = message.get("remote_jid")
                if new_remote_jid and new_remote_jid != existing.remote_jid:
                    if (
                        existing.remote_jid
                        and existing.remote_jid.endswith("@lid")
                        and new_remote_jid.endswith("@s.whatsapp.net")
                    ):
                        existing.remote_jid = new_remote_jid
                    elif not existing.remote_jid:
                        existing.remote_jid = new_remote_jid
                if "r2_media_url" in message:
                    existing.r2_media_url = message.get("r2_media_url")
                if "participant" in message:
                    existing.participant = message.get("participant")
                if "peer_pn" in message:
                    existing.peer_pn = normalize_number(message.get("peer_pn"))
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                return

            message_record = Message(
                message_id=msg_id,
                direction=message.get("direction", "incoming"),
                admin_number=normalize_number(message.get("admin_number", "")),
                cx_number=normalize_number(message.get("cx_number", "")),
                content=message.get("content", ""),
                clean_content=message.get("clean_content", ""),
                timestamp=msg_ts,
                remote_jid=message.get("remote_jid"),   
                device=message.get("device", "baileys"),
                issent=message.get("issent", False),
                isread=message.get("isread", False),
                r2_media_url=message.get("r2_media_url"),
                participant=message.get("participant"),
                peer_pn=normalize_number(message.get("peer_pn")) if message.get("peer_pn") else None,
            )

            db.add(message_record)
            try:
                db.commit()
            except IntegrityError:
                # Another worker inserted the same message concurrently
                db.rollback()
                return
            db.refresh(message_record)

            # Only log when actually inserted
            print(
                f"Message saved for {number} (ignore_flag={ignore_flag}) "
                f"at {message_record.timestamp.strftime('%Y-%m-%d %H:%M:%S')} IST"
            )
    except Exception as e:
        print(f"Error logging message: {e}")

def start_client_for_number(number):
    try:
        result = subprocess.run(
            ["node", "./services/wa_baileys_helper.js", str(number)],
            capture_output=True,
            text=True,
            check=True
        )
        print(result.stdout)
        data = json.loads(result.stdout)
        print(data)
        print(1)
        clients[number] = {"ready": True, "qr": None}
        # Note: log_message is called from the JavaScript helper via HTTP API
        tracked_numbers.add(number)
        return {"success": True, "data": data}
    except subprocess.CalledProcessError as e:
        print("Node.js error output:", e.stderr)
        return {"error": str(e), "stderr": e.stderr, "success": False}
    except json.JSONDecodeError:
        print("Error decoding JSON from wa_baileys_helper.js output.")
        return {"error": "Invalid JSON output", "success": False}

def get_client_status(number):
    if number not in clients:
        return {"status": "not_started"}
    if clients[number]["ready"]:
        return {"status": "ready"}
    return {"status": "qr", "qrImage": clients[number]["qr"]}


def register_number_to_session(session_id: str, phone_number: str):
    """Register a phone number to a session. If the number already has an active session, stop it first."""
    if not phone_number or not session_id:
        return

    # 1. Check for conflicts
    old_session_id = number_to_session.get(phone_number)
    if old_session_id and old_session_id != session_id:
        print(f"⚠️ Conflict detected: {phone_number} moved from {old_session_id[:8]} to {session_id[:8]}")
        # Stop the old session process in a separate thread to avoid blocking registration
        threading.Thread(target=stop_session, args=(old_session_id,), daemon=True).start()

    # 2. Update mappings
    if session_id not in session_to_numbers:
        session_to_numbers[session_id] = set()
    
    session_to_numbers[session_id].add(phone_number)
    number_to_session[phone_number] = session_id
    tracked_numbers.add(phone_number)

def clean_message_content(text):
    """
    Clean message content by removing [media omitted] markers while preserving 
    Unicode characters (emojis, non-Latin scripts, etc).
    """
    if not text or not isinstance(text, str):
        return ""
    import re
    # Remove media omitted markers (case-insensitive)
    cleaned = re.sub(r"\[?media omitted\]?", "", text, flags=re.I)
    return cleaned.strip()
    
# new headless session 
def initialize_headless_session():
    """Initialize WhatsApp session and return QR code"""
    import uuid
    import time
    import subprocess
    import json
    import os

    session_id = str(uuid.uuid4())
    base_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(base_dir, 'wa_qr_helper.js')

    try:
        print(f"\n{'='*60}")
        print(f"🚀 Starting WhatsApp QR helper for session: {session_id}")
        print(f"{'='*60}\n")
        
        proc = subprocess.Popen(
            ["node", script_path, session_id],
            cwd=base_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        qr_data = None
        start = time.time()
        
        timeout_s = 60
        
        while True:
            if (time.time() - start) > timeout_s:
                proc.kill()
                raise Exception(f"QR helper timeout after {timeout_s} seconds")
            
            line = proc.stdout.readline() if proc.stdout else ''
            if not line:
                time.sleep(0.1)
                if proc.poll() is not None:
                    print(f"❌ Process exited with code: {proc.returncode}")
                    raise Exception(f"QR helper process exited unexpectedly. Exit code: {proc.returncode}")
                continue
            
            line = line.strip()
            if not line:
                continue
            
            # Print EVERY line from Node.js
            print(f"[Node.js] {line}")
            
            try:
                data = json.loads(line)
                status = data.get('status')
                
                if status == 'qr' and data.get('qrCode'):
                    print(f"\n✅ Got QR code for session {session_id}\n")
                    
                    clients[session_id] = {
                        "phone_numbers": set(),
                        "verified": False,
                        "created_at": time.time(),
                        "process": proc,
                        "status": "qr_generated"
                    }
                    
                    # Start monitoring
                    import threading
                    thread = threading.Thread(
                        target=monitor_qr_authentication,
                        args=(session_id, proc),
                        daemon=True
                    )
                    thread.start()
                    print(f"🔍 Started monitoring thread for session {session_id}\n")
                    
                    return {
                        "sessionId": session_id,
                        "qrCode": data.get("qrCode"),
                        "phoneNumber": None,
                        "alreadyAuthenticated": False
                    }
                
                if status in ('already_authenticated', 'ready'):
                    phone_number = data.get('phoneNumber')
                    print(f"\n✅ Already authenticated with {phone_number}\n")
                    
                    clients[session_id] = {
                        "phone_numbers": set([phone_number]) if phone_number else set(),
                        "verified": True,
                        "created_at": time.time(),
                        "process": None,
                        "status": "authenticated"
                    }
                    
                    if phone_number:
                        register_number_to_session(session_id, phone_number)
                    
                    return {
                        "sessionId": session_id,
                        "qrCode": None,
                        "phoneNumber": phone_number,
                        "alreadyAuthenticated": True
                    }

            except json.JSONDecodeError:
                continue

    except Exception as e:
        print(f"\n❌ Error initializing session: {str(e)}\n")
        if 'proc' in locals() and proc.poll() is None:
            proc.kill()
        raise e


def monitor_qr_authentication(session_id, proc):
    """Monitor for QR code scan completion"""
    import json
    import time
    
    print(f"🔍 [Monitor {session_id[:8]}...] Started monitoring")
    
    try:
        while True:
            line = proc.stdout.readline() if proc.stdout else ''
            if not line:
                if proc.poll() is not None:
                    print(f"⚠️ [Monitor {session_id[:8]}...] Process ended (exit code: {proc.returncode})")
                    break
                time.sleep(0.1)
                continue
            
            line = line.strip()
            if not line:
                continue
            
            # Print everything from monitor thread too
            print(f"[Monitor {session_id[:8]}...] {line}")
            
            try:
                data = json.loads(line)
                status = data.get('status')
                message = data.get('message', '')
                
                if status == 'info':
                    print(f"ℹ️ [Monitor {session_id[:8]}...] {message}")
                
                if status in ('ready', 'already_authenticated'):
                    phone_number = data.get('phoneNumber')
                    print(f"\n✅✅✅ [Monitor {session_id[:8]}...] AUTHENTICATED! Phone: {phone_number}\n")
                    
                    if session_id in clients:
                        clients[session_id]['verified'] = True
                        clients[session_id]['status'] = 'authenticated'
                        if phone_number:
                            register_number_to_session(session_id, phone_number)
                            print(f"📝 Registered phone {phone_number} to session {session_id[:8]}...")
                    
                    break
                    
            except json.JSONDecodeError:
                continue
                
    except Exception as e:
        print(f"❌ [Monitor {session_id[:8]}...] Error: {e}")
    finally:
        print(f"🏁 [Monitor {session_id[:8]}...] Monitoring ended")
        if session_id in clients:
            clients[session_id]['process'] = None


def reset_qr_session(session_id: str):
    """Stop an existing QR session (if any) so a fresh QR can be generated."""
    try:
        sess = clients.get(session_id)
        if not sess:
            return {"success": True, "message": "Session not found", "sessionId": session_id}

        proc = sess.get("process")
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

        base_dir = os.path.dirname(os.path.abspath(__file__))
        bridge_port_file = os.path.join(base_dir, ".baileys_auth", session_id, "bridge_port.txt")
        try:
            if os.path.exists(bridge_port_file):
                os.remove(bridge_port_file)
        except Exception:
            pass

        clients.pop(session_id, None)

        numbers = session_to_numbers.pop(session_id, set())
        for n in numbers:
            number_to_session.pop(n, None)

        return {"success": True, "message": "Session reset", "sessionId": session_id}
    except Exception as e:
        return {"success": False, "message": str(e), "sessionId": session_id}


def check_session_status(session_id):
    """Check if WhatsApp session has been verified and manage streaming process"""
    from app.model.wa_sessions import WhatsAppSession
    
    # Use a per-session lock to prevent race conditions during streamer startup
    lock = get_session_lock(session_id)
    
    with lock:
        db = SessionLocal()
        try:
            if session_id not in clients:
                # If not in memory but DB has it and it looks okay, restore it
                session_db = db.query(WhatsAppSession).filter(WhatsAppSession.session_id == session_id).first()
                if not session_db:
                    # Create the row if it doesn't exist
                    session_db = WhatsAppSession(session_id=session_id)
                    db.add(session_db)
                    db.commit()

                clients[session_id] = {
                    "verified": session_db.verified,
                    "status": session_db.status,
                    "stream_started": session_db.stream_started,
                    "stream_pid": session_db.stream_pid,
                    "is_active": session_db.is_active
                }

            session = clients[session_id]
            base_dir = os.path.dirname(os.path.abspath(__file__))

            # Prefer Baileys creds.json as source of truth for auth
            baileys_folder = os.path.join(base_dir, ".baileys_auth", session_id)
            baileys_creds = os.path.join(baileys_folder, "creds.json")
            
            if os.path.exists(baileys_creds):
                session["verified"] = True
                
                # Sync with DB
                session_db = db.query(WhatsAppSession).filter(WhatsAppSession.session_id == session_id).first()
                if session_db:
                    session_db.verified = True
                    db.commit()

                phone_numbers = session_to_numbers.get(session_id, set())

                # If no phones registered, try to get from helper or creds
                if not phone_numbers:
                    try:
                        with open(baileys_creds, "r", encoding="utf-8") as f:
                            creds = json.load(f)
                        me_id = None
                        try:
                            me_id = (creds.get("me") or {}).get("id")
                        except Exception:
                            me_id = None
                        if me_id:
                            derived_phone = str(me_id).split("@")[0].split(":")[0].split("-")[0]
                            derived_phone = "".join([c for c in derived_phone if c.isdigit()])
                            if derived_phone:
                                register_number_to_session(session_id, derived_phone)
                                phone_numbers = session_to_numbers.get(session_id, set())
                                # Update DB
                                if session_db:
                                    session_db.phone_number = derived_phone
                                    db.commit()
                    except Exception:
                        pass

                    # Fallback: Run QR helper to verify status if still missing phone
                    if not phone_numbers:
                        try:
                            script_path = os.path.join(base_dir, 'wa_qr_helper.js')
                            # Prepare environment and inject BACKEND_URL for the QR helper
                            env_copy = os.environ.copy()
                            backend_url = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")
                            env_copy["BACKEND_URL"] = backend_url

                            result = subprocess.run(
                                ["node", script_path, session_id],
                                capture_output=True,
                                text=True,
                                check=True,
                                timeout=60,
                                cwd=base_dir,
                                env=env_copy
                            )
                            
                            for line in result.stdout.strip().split('\n'):
                                try:
                                    data = json.loads(line)
                                    if data.get('status') in ('already_authenticated', 'ready'):
                                        new_phone = data.get('phoneNumber')
                                        if new_phone:
                                            register_number_to_session(session_id, new_phone)
                                            phone_numbers = session_to_numbers.get(session_id, set())
                                            if session_db:
                                                session_db.phone_number = new_phone
                                                db.commit()
                                except Exception:
                                    continue
                        except Exception as e:
                            print(f"Warning: Failed to get phone number from helper: {e}")

                # Start message streaming if not already running OR if the previous process is unhealthy
                current_pid = session.get("stream_pid")
                is_really_active, reason = is_session_really_active(session_id)

                # Sync memory/DB if bridge is alive
                if is_really_active:
                    session["verified"] = True
                    session["is_active"] = True
                    session["status"] = "connected"
                    session["stream_started"] = True
                    
                    if session_db:
                        session_db.verified = True
                        session_db.is_active = True
                        session_db.status = "connected"
                        session_db.stream_started = True
                        db.commit()

                if not session.get("stream_started") or not is_really_active:
                    # CRITICAL FIX: If creds are missing, we CANNOT restart. Mark as expired.
                    if reason == "creds_missing":
                        print(f"🧹 [Lock {session_id[:8]}...] Creds missing for verified session. Marking unverified.")
                        session["verified"] = False
                        session["is_active"] = False
                        if session_db:
                            session_db.verified = False
                            session_db.is_active = False
                            db.commit()
                        return {"verified": False, "expired": True, "reason": "creds_missing"}

                    # If process is alive but unhealthy (e.g. ghost), kill it first
                    # GRACE PERIOD: Do not kill if it just started (give it 45s to initialize)
                    started_at = session.get("_stream_started_at")
                    still_starting = session.get("status") == "starting"
                    within_grace = started_at and (time.time() - started_at) < 45

                    if (
                        current_pid
                        and is_pid_alive(current_pid)
                        and not is_really_active
                        and still_starting
                        and within_grace
                        and reason in ("port_missing", "bridge_unreachable")
                    ):
                        print(f"⏳ [Lock {session_id[:8]}...] Process {current_pid} is initializing. Respecting grace period.")
                        return {
                            "verified": True,
                            "expired": False,
                            "phoneNumbers": list(session_to_numbers.get(session_id, set())),
                            "status": "starting",
                            "is_active": False,
                            "stream_started": True,
                            "stream_pid": current_pid,
                            "health_reason": reason,
                        }

                    if current_pid and is_pid_alive(current_pid) and not is_really_active:
                        if reason == "whatsapp_disconnected":
                            print(f"⏳ [Lock {session_id[:8]}...] Process {current_pid} is alive but WhatsApp is disconnected. Waiting for auto-reconnect.")
                            return {
                                "verified": True,
                                "expired": False,
                                "phoneNumbers": list(session_to_numbers.get(session_id, set())),
                                "status": session.get("status", "reconnecting"),
                                "is_active": False,
                                "stream_started": True,
                                "stream_pid": current_pid,
                                "health_reason": reason,
                            }
                            
                        print(f"⚠️ [Lock {session_id[:8]}...] Process {current_pid} is alive but unhealthy ({reason}). Killing for restart.")
                        try:
                            if os.name == 'nt':
                                subprocess.run(["taskkill", "/F", "/T", "/PID", str(current_pid)], capture_output=True)
                            else:
                                import signal
                                os.kill(current_pid, signal.SIGKILL)
                        except Exception:
                            pass
                    try:
                        print(f"🚀 [Lock {session_id[:8]}...] Starting message stream process")
                        logs_dir = os.path.join(base_dir, "logs")
                        os.makedirs(logs_dir, exist_ok=True)
                        log_path = os.path.join(logs_dir, f"baileys_{session_id}.log")
                        log_fh = open(log_path, "a", encoding="utf-8", buffering=1)

                        numbers = session_to_numbers.get(session_id, set())
                        first_number = next(iter(numbers), "")
                        
                        # Prepare environment and inject config
                        env_copy = os.environ.copy()
                        backend_url = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")
                        env_copy["BACKEND_URL"] = backend_url
                        env_copy["BRIDGE_HOST"] = BRIDGE_HOST
                        
                        stream_run_id = f"{session_id}_{int(time.time())}"
                        env_copy["WA_STREAM_RUN_ID"] = stream_run_id

                        streamer = subprocess.Popen(
                            ["node", os.path.join(base_dir, 'wa_baileys_helper.js'), session_id, first_number],
                            cwd=base_dir,
                            stdout=log_fh,
                            stderr=subprocess.STDOUT,
                            env=env_copy
                        )
                        
                        session["stream_started"] = True
                        session["stream_pid"] = streamer.pid
                        session["stream_log_path"] = log_path
                        session["_stream_log_fh"] = log_fh
                        session["is_active"] = False # Not fully active until bridge connects
                        session["status"] = "starting"
                        session["_stream_started_at"] = time.time()
                        
                        # Update DB
                        if session_db:
                            session_db.stream_run_id = stream_run_id
                            session_db.stream_started = True
                            session_db.stream_pid = streamer.pid
                            session_db.stream_log_path = log_path
                            session_db.is_active = False
                            session_db.status = "starting"
                            session_db.last_health_reason = "stream_process_started"
                            db.commit()
                            
                    except Exception as e:
                        print(f"Warning: Failed to start message streaming: {e}")
                        session["stream_started"] = False

                return {
                    "verified": True,
                    "expired": False,
                    "phoneNumbers": list(session_to_numbers.get(session_id, set())),
                    "status": session.get("status", "authenticated"),
                    "is_active": bool(session.get("is_active", False)),
                    "stream_started": bool(session.get("stream_started", False)),
                    "stream_pid": session.get("stream_pid"),
                }

            # Fallback legacy check removed. 
            # If no auth files found but flagged as verified, treat as expired
            if session.get("verified"):
                if session_id in clients:
                    del clients[session_id]
                return {"verified": False, "expired": True, "reason": "auth_files_missing"}

            return {"verified": False, "expired": False, "phoneNumber": list(session_to_numbers.get(session_id, set()))}

        except Exception as e:
            logger.error(f"❌ Error checking session status {session_id}: {str(e)}\n{traceback.format_exc()}")
            return {"verified": False, "expired": True, "error": str(e)}
        finally:
            db.close()

# Start background cleanup task
def run_supervisor_loop():
    import time
    while True:
        try:
            cleanup_stale_sessions()
        except Exception as e:
            print(f"Supervisor loop error: {e}")
        time.sleep(60) # Every 1 minute

supervisor_thread = threading.Thread(target=run_supervisor_loop, daemon=True)
supervisor_thread.start()


def stop_session(session_id):
    """Gracefully shut down a specific WhatsApp session by ID."""
    import os
    import shutil
    import subprocess
    import requests
    import time
    from datetime import datetime
    from app.db.database import SessionLocal
    from app.model.wa_sessions import WhatsAppSession

    lock = get_session_lock(session_id)
    with lock:
        db = SessionLocal()
        try:
            session_data = clients.get(session_id, {})
            
            # 1. Kill the QR process if running
            qr_proc = session_data.get("process")
            if qr_proc is not None:
                try:
                    if qr_proc.poll() is None:
                        qr_proc.kill()
                except Exception:
                    pass

            # 2. Try Graceful shutdown via bridge
            base_dir = os.path.dirname(os.path.abspath(__file__))
            port_file = os.path.join(base_dir, ".baileys_auth", session_id, "bridge_port.txt")
            graceful_success = False
            
            # Resolve PID early for monitoring
            stream_pid = session_data.get("stream_pid")
            if not stream_pid:
                session_db = db.query(WhatsAppSession).filter(WhatsAppSession.session_id == session_id).first()
                if session_db:
                    stream_pid = session_db.stream_pid

            if os.path.exists(port_file):
                try:
                    with open(port_file, "r") as f:
                        port = f.read().strip()
                    if port:
                        # Request graceful stop (with logout)
                        try:
                            requests.post(f"http://{BRIDGE_HOST}:{port}/stop", json={"logout": True}, timeout=5)
                        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                            # Connection error usually means it closed immediately - check PID below
                            pass
                            
                        # Wait up to 10 seconds for process to exit
                        if stream_pid:
                            for _ in range(10):
                                time.sleep(1)
                                if not is_pid_alive(stream_pid):
                                    graceful_success = True
                                    break
                except Exception:
                    pass

            # 3. Force Kill fallback if still running after timeout
            # Safety Check: Never kill the current process (FastAPI)
            current_pid = os.getpid()
            if stream_pid and stream_pid != current_pid and not graceful_success and is_pid_alive(stream_pid):
                try:
                    if os.name == 'nt':
                        # Windows forced tree kill
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(stream_pid)], capture_output=True)
                    else:
                        # Unix forced kill
                        import signal
                        os.kill(stream_pid, signal.SIGKILL)
                except Exception:
                    pass

            # 4. Close the stream log file handle
            stream_fh = session_data.get("_stream_log_fh")
            if stream_fh:
                try:
                    stream_fh.close()
                except Exception:
                    pass
                
            # 5. Update DB Status
            session_db = db.query(WhatsAppSession).filter(WhatsAppSession.session_id == session_id).first()
            if session_db:
                session_db.is_active = False
                session_db.stream_started = False
                session_db.stream_pid = None
                session_db.status = "disconnected"
                session_db.last_disconnected_at = datetime.utcnow()
                db.commit()

            if session_id in clients:
                del clients[session_id]

            # 6. Clean up memory mapping for phone numbers
            numbers_to_remove = [num for num, sid in number_to_session.items() if sid == session_id]
            for num in numbers_to_remove:
                number_to_session.pop(num, None)
                tracked_numbers.discard(num)
            
            if session_id in session_to_numbers:
                del session_to_numbers[session_id]

        finally:
            db.close()
        
        # 7. Clean up auth folders
        auth_folders = [
            os.path.join(base_dir, ".wwebjs_auth", session_id),
            os.path.join(base_dir, ".baileys_auth", session_id)
        ]
        for folder in auth_folders:
            if os.path.exists(folder):
                try:
                    shutil.rmtree(folder, ignore_errors=True)
                except Exception:
                    pass
    return {"success": True, "message": f"Session {session_id} stopped"}

def stop_tracking_number(number):
    """Stop tracking a WhatsApp number and gracefully shut down the associated session if no numbers left."""
    session_id = number_to_session.get(number)
    
    if session_id:
        # Check if other numbers belong to this session
        other_numbers = [num for num, sid in number_to_session.items() if sid == session_id and num != number]
        
        if not other_numbers:
            # Last number, stop entire session
            return stop_session(session_id)
        else:
            # Just remove this number from tracking
            tracked_numbers.discard(number)
            number_to_session.pop(number, None)
            if session_id in session_to_numbers:
                session_to_numbers[session_id].discard(number)
            return {"success": True, "message": f"Stopped tracking {number}, session remains active for others"}

    return {"success": False, "error": "Number not associated with any session"}

def send_baileys_message(session_id: str, jid: str, text: str, quoted=None, mentions: list = None):
    """Send a text message via the Baileys bridge for a specific session and log it."""
    import requests
    from app.db.database import SessionLocal
    from app.model.wa_sessions import WhatsAppSession
    from app.model.message import Message
    from datetime import datetime

    # 1. Normalize JID
    target_jid = normalize_jid(jid)

    base_dir = os.path.dirname(os.path.abspath(__file__))
    session_dir = os.path.join(base_dir, ".baileys_auth", session_id)
    port_file = os.path.join(session_dir, "bridge_port.txt")

    def _remove_stale_port_file():
        try:
            if os.path.exists(port_file):
                os.remove(port_file)
        except Exception:
            pass

    if not os.path.exists(port_file):
        return {
            "success": False,
            "error": f"Bridge port file not found for session {session_id}. Is the session running?"
        }

    try:
        with open(port_file, "r", encoding="utf-8") as f:
            port = f.read().strip()

        if not port:
            _remove_stale_port_file()
            return {
                "success": False,
                "error": f"Bridge port file is empty for session {session_id}"
            }

        base_url = f"http://{BRIDGE_HOST}:{port}"

        # Health-check
        try:
            health = requests.get(f"{base_url}/health", timeout=3)
            health_payload = health.json() if health.status_code == 200 else {}
            if health.status_code != 200 or not health_payload.get("is_connected", False):
                return {"success": False, "error": f"Baileys session {session_id} is not connected"}
        except Exception:
            _remove_stale_port_file()
            return {"success": False, "error": "Bridge is not reachable"}

        # Step 2: send message
        send_payload = {
            "jid": target_jid,
            "text": text,
            "quoted": quoted
        }
        if mentions:
            send_payload["mentions"] = mentions

        response = requests.post(
            f"{base_url}/send",
            json=send_payload,
            timeout=10
        )

        try:
            payload = response.json()
        except Exception:
            payload = {"success": False, "error": f"Invalid bridge response (HTTP {response.status_code})"}

        if payload.get("success"):
            # Step 3: PERSIST OUTGOING MESSAGE
            db = SessionLocal()
            try:
                # Find admin number from session
                session_db = db.query(WhatsAppSession).filter(WhatsAppSession.session_id == session_id).first()
                admin_number = session_db.phone_number if session_db else None
                
                if not admin_number:
                    # Fallback to local memory if DB record not found
                    admin_number = list(session_to_numbers.get(session_id, [None]))[0]

                if admin_number:
                    # Capture Baileys key ID
                    # Payload format: { success: true, status: 'sent', result: { key: { id: ... } } }
                    msg_id = payload.get("result", {}).get("key", {}).get("id") or f"OUT_{int(datetime.utcnow().timestamp())}"
                    
                    new_msg = Message(
                        message_id=msg_id,
                        admin_number=normalize_number(admin_number),
                        cx_number=normalize_number(target_jid),
                        content=text,
                        direction="outgoing",
                        issent=True,
                        isread=False,
                        timestamp=datetime.utcnow(),
                        device="baileys",
                        remote_jid=target_jid
                    )
                    db.add(new_msg)
                    db.commit()
                    print(f"✅ Logged outgoing message: {msg_id}")
            except Exception as db_err:
                print(f"⚠️ Failed to log outgoing message to DB: {db_err}")
                db.rollback()
            finally:
                db.close()

        return payload

    except Exception as e:
        return {"success": False, "error": str(e)}
