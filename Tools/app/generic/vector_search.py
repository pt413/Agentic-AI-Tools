import re
import json
from sqlalchemy import text, select
from app.db.database import engine, SessionLocal
from app.db.redis import get_redis_client
from app.model.all_data_model import CustomerRecord
from app.generic.embedding import generate_embedding
# from app.redis_client import get_redis_client  # make sure you have async redis client
from datetime import datetime

# Regex patterns
EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
BOOKING_ID_RE = re.compile(r"\b(\d{3,8})\b")
PHONE_RE = re.compile(r"\b\d{8,15}\b")

CACHE_TTL = 3600  # 1 hour cache

# -----------------------------
# Detect identifiers in query
# -----------------------------
async def detect_identifiers(query: str):
    """Return dictionary with detected booking_id, email, phone (or None)."""
    email = EMAIL_RE.search(query)
    bid = BOOKING_ID_RE.search(query)
    phone = PHONE_RE.search(query)
    return {
        "email": email.group(0) if email else None,
        "booking_id": bid.group(1) if bid else None,
        "phone": phone.group(0) if phone else None,
    }

# -----------------------------
# Redis + PostgreSQL fetch
# -----------------------------
async def get_booking_by_id(booking_id: str):
    """Return booking by ID, with Redis caching."""
    redis_client = await get_redis_client()
    cache_key = f"booking:{booking_id}"

    # Try cache first
    cached_data = await redis_client.get(cache_key)
    if cached_data:
        return json.loads(cached_data)

    # Query DB
    with SessionLocal() as db:
        result = db.execute(
            select(CustomerRecord).where(CustomerRecord.booking_id == booking_id)
        ).scalars().first()

        if result:
            # Manually convert ORM object to dict
            data_dict = {
                "booking_id": result.booking_id,
                "booking_json": result.booking_json,
                "emails_json": result.emails_json,
                "whatsapp_json": result.whatsapp_json,
                "call_logs_json": result.call_logs_json
            }

            # Cache the dict
            await redis_client.setex(cache_key, CACHE_TTL, json.dumps(data_dict))
            return data_dict

    return None

async def get_booking_by_email(email: str):
    """Fetch booking by email from DB."""
    redis_client = await get_redis_client()
    cache_key = f"email_booking:{email}"
    
    # Try cache first
    cached_data = await redis_client.get(cache_key)
    if cached_data:
        return json.loads(cached_data)
    
    with SessionLocal() as db:
        row = db.execute(
            text("""
                SELECT booking_id, booking_json, emails_json, whatsapp_json, call_logs_json
                FROM customer_record
                WHERE (booking_json->'all_emails'->>'primary') = :email
                   OR primary_email = :email
                LIMIT 1
            """),
            {"email": email}
        ).first()
        if row:
            # Convert to dict similar to ORM
            result = {
                "booking_id": row[0],
                "booking_json": row[1],
                "emails_json": row[2],
                "whatsapp_json": row[3],
                "call_logs_json": row[4],
                
            }
            
            # Cache the result
            await redis_client.setex(cache_key, CACHE_TTL, json.dumps(result))
            return result
    return None

async def get_booking_by_phone(phone: str):
    """Fetch booking by phone number from DB."""
    redis_client = await get_redis_client()
    cache_key = f"phone_booking:{phone}"
    
    # Try cache first
    cached_data = await redis_client.get(cache_key)
    if cached_data:
        return json.loads(cached_data)
    
    with SessionLocal() as db:
        row = db.execute(
            text("""
                SELECT booking_id, booking_json, emails_json, whatsapp_json, call_logs_json
                FROM customer_record
                WHERE (booking_json->'tenant_phones' ? :phone)
                   OR (whatsapp_json::text LIKE :phone_like)
                   OR (call_logs_json::text LIKE :phone_like)
                LIMIT 1
            """),
            {"phone": phone, "phone_like": f"%{phone}%"}
        ).first()
        if row:
            # Convert to dict similar to ORM
            result = {
                "booking_id": row[0],
                "booking_json": row[1],
                "emails_json": row[2],
                "whatsapp_json": row[3],
                "call_logs_json": row[4],
            }
            
            # Cache the result
            await redis_client.setex(cache_key, CACHE_TTL, json.dumps(result))
            return result
    return None

# -----------------------------
# Vector search (only used when no identifiers found)
# -----------------------------
async def vector_search_multi(query: str, vec: list, top_k: int = 2):
    vec_literal = "ARRAY[" + ",".join(str(float(x)) for x in vec) + "]::vector"
    sql = f"""
        SELECT booking_id, booking_json, emails_json, whatsapp_json, call_logs_json,
               0.5 * (1 - (booking_vector <=> {vec_literal})) +
               0.3 * (1 - (email_vector <=> {vec_literal})) +
               0.2 * (1 - (whatsapp_vector <=> {vec_literal})) AS score
        FROM customer_record
        ORDER BY score DESC
        LIMIT {int(top_k)}
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).fetchall()

    results = []
    for r in rows:
        results.append({
            "booking_id": r[0],
            "booking_json": r[1],
            "emails_json": r[2],
            "whatsapp_json": r[3],
            "call_logs_json": r[4],
            "score": float(r[5])
        })
    return results

# -----------------------------
# Format booking dict
# -----------------------------
# -----------------------------
# Format booking dict - Updated for your nested structure
# -----------------------------
def format_booking_data(raw):
    booking_data = raw.get("booking_json") or {}

    # Ensure booking_array is always a list
    booking_array = booking_data.get("booking", [])
    if isinstance(booking_array, dict):
        booking_array = [booking_array]
    if not isinstance(booking_array, list):
        booking_array = []

    booking = booking_array[0] if booking_array else {}

    tenant = {
        "name": booking.get("traveller_name"),
        "email": booking.get("contact_email"),
        "phones": [booking.get("traveller_contact_num")] if booking.get("traveller_contact_num") else [],
    }

    property_info = {
        "id": None,
        "name": None
    }

    stay = {
        "start": booking.get("travel_from_date"),
        "end": booking.get("travel_to_date")
    }

    calls = []
    for call in raw.get("call_logs_json", []) or booking_data.get("communications", {}).get("calls", []):
        calls.append({
            "date": call.get("date"),
            "number": call.get("number"),
            "by": call.get("by"),
            "status": call.get("status"),
            "duration_sec": call.get("duration_sec")
        })

    whatsapp = []
    for msg in raw.get("whatsapp_json", []) or booking_data.get("communications", {}).get("whatsapp", []):
        whatsapp.append({
            "date": msg.get("date"),
            "from": msg.get("from") or msg.get("sender") or "unknown",
            "message": msg.get("message") or msg.get("content")
        })

    risk_notes = booking_data.get("risk_notes", [])

    return {

        "booking_status": booking.get("booking_status"),
        "booking_json": booking_data,
        "communications": {
            "calls": calls,
            "whatsapp": whatsapp
        },
        "risk_notes": risk_notes
    }

# -----------------------------
# Main retrieve function - Optimized
# -----------------------------
async def retrieve_for_query(query: str, top_k: int = 5, vec: list = None):
    # Step 1: Detect identifiers in the query
    identifiers = await detect_identifiers(query)
    
    # Step 2: If any identifier is found, try to fetch directly from DB
    if identifiers["booking_id"]:
        booking = await get_booking_by_id(identifiers["booking_id"])
        if booking:
            return [format_booking_data(booking)]
    
    if identifiers["email"]:
        booking = await get_booking_by_email(identifiers["email"])
        if booking:
            return [format_booking_data(booking)]
    
    if identifiers["phone"]:
        booking = await get_booking_by_phone(identifiers["phone"])
        if booking:
            return [format_booking_data(booking)]
    
    # Step 3: If no identifiers found or no direct matches, perform vector search
    if vec is None:
        vec = generate_embedding(query)
    
    # Perform vector search
    vector_results = await vector_search_multi(query, vec, top_k)
    
    # Format results
    formatted_results = [format_booking_data(r) for r in vector_results]
    
    return formatted_results

# Helper function for cosine distance
def cosine_distance(a, b):
    """
    Compute cosine distance between two vectors.
    Cosine distance = 1 - cosine similarity
    """
    a = np.array(a, dtype=float)
    b = np.array(b, dtype=float)
    
    # avoid division by zero
    if np.linalg.norm(a) == 0 or np.linalg.norm(b) == 0:
        return 1.0  
    
    cos_sim = np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))
    return 1 - cos_sim


# CACHE_TTL = 86400  # 1 day TTL for preloaded cache

async def preload_all_bookings():
    redis_client = await get_redis_client()

    with SessionLocal() as db:
        all_bookings = db.query(CustomerRecord).all()

    for booking in all_bookings:
        booking_dict = {
            "booking_id": booking.booking_id,
            "booking_json": booking.booking_json,
            "emails_json": booking.emails_json,
            "whatsapp_json": booking.whatsapp_json,
            "call_logs_json": booking.call_logs_json
        }

        # Store booking in Redis
        await redis_client.setex(f"booking:{booking.booking_id}", CACHE_TTL, json.dumps(booking_dict))

        # Compute embedding for booking (you can choose which fields to embed)
        text_to_embed = json.dumps(booking_dict)
        embedding = generate_embedding(text_to_embed)

        # Store embedding in Redis
        await redis_client.setex(f"embedding:{booking.booking_id}", CACHE_TTL, json.dumps(embedding))

    print(f"Preloaded {len(all_bookings)} bookings into Redis!")

# Run the preload
# asyncio.run(preload_all_bookings())
