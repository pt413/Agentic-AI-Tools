import json
import httpx
from sqlalchemy.orm import Session
# from app.db.session import SessionLocal
from app.db.database import SessionLocal
# from app.models.customer_record import CustomerRecord
from app.model.all_data_model import CustomerRecord
# from app.generic.embeddings import generate_embedding  
# f# your own embedding fn
from app.generic.embedding import generate_embedding
from app.generic.data_collecter import get_booking_details  # your own fn
# from app.utils import json_converter  # your own JSON date converter
from app.generic.converter import json_converter  # your own JSON date converter


# --- helper to convert SQLAlchemy model → dict ---
def safe_vector(vec):
    if vec is None:
        return None
    # covers both np.ndarray and pgvector.Vector
    return [float(x) for x in list(vec)]

def record_to_dict(record: CustomerRecord) -> dict:
    return {
        "id": record.id,
        "booking_id": record.booking_id,
        "booking_json": record.booking_json,
        "emails_json": record.emails_json,
        "whatsapp_json": record.whatsapp_json,
        "call_logs_json": record.call_logs_json,
        "booking_status": record.booking_status,
        "primary_contact": record.primary_contact,
        "primary_email": record.primary_email,
        "prop_id": record.prop_id,
        "prop_name": record.prop_name,
        "travel_from_date": str(record.travel_from_date) if record.travel_from_date else None,
        "travel_to_date": str(record.travel_to_date) if record.travel_to_date else None,
        "updated_at": str(record.updated_at) if record.updated_at else None,
        "booking_vector": safe_vector(record.booking_vector),
        "email_vector": safe_vector(record.email_vector),
        "whatsapp_vector": safe_vector(record.whatsapp_vector),
        "calls_vector": safe_vector(record.calls_vector),
        "created_at": str(record.created_at) if record.created_at else None,
    }


async def app_booking_data():
    """
    Fetch bookings from external API, generate embeddings, insert into PostgreSQL,
    return a JSON-safe list of inserted records.
    """
    # url = "https://www.rentmystay.com/User/get_addedon_details/arnab.usa.2020@gmail.com"
    url = "https://www.rentmystay.com/User/get_addedon_details"
    headers = {"Authorization": "687ba37d3241d"}

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        return None

    data = resp.json()

    inserted_records = []
    with SessionLocal() as db:  # type: Session
        for booking_data in data:
            booking_id = booking_data.get("all_booking_ids")
            if not booking_id:
                continue

            existing = await get_booking_details(booking_id)
            if not existing:
                continue

            booking_json = existing.get("booking")
            emails_json = existing.get("emails")
            whatsapp_json = existing.get("whatsapp")
            call_logs_json = existing.get("call_logs")

            # Convert to text for embeddings
            booking_text = json.dumps(booking_json, default=json_converter, ensure_ascii=False)
            emails_text = json.dumps(emails_json, default=json_converter, ensure_ascii=False)
            whatsapp_text = json.dumps(whatsapp_json, default=json_converter, ensure_ascii=False)
            calls_text = json.dumps(call_logs_json, default=json_converter, ensure_ascii=False)

            # embeddings — generate_embedding must return a list or np.array
            booking_emb = generate_embedding(booking_text)
            email_emb = generate_embedding(emails_text)
            whatsapp_emb = generate_embedding(whatsapp_text)
            calls_emb = generate_embedding(calls_text)

            bj = booking_json or {}
            record = CustomerRecord(
                booking_id=booking_id,
                booking_json=booking_json,
                emails_json=emails_json,
                whatsapp_json=whatsapp_json,
                call_logs_json=call_logs_json,
                booking_status=bj.get("booking_status"),
                primary_contact=bj.get("all_contacts", {}).get("primary"),
                primary_email=bj.get("all_emails", {}).get("primary"),
                prop_id=bj.get("prop_id"),
                prop_name=bj.get("prop_name"),
                travel_from_date=bj.get("travel_from_date"),
                travel_to_date=bj.get("travel_to_date"),
                updated_at=bj.get("updated_at"),
                booking_vector=booking_emb,
                email_vector=email_emb,
                whatsapp_vector=whatsapp_emb,
                calls_vector=calls_emb
            )

            db.add(record)
            db.commit()
            db.refresh(record)

            inserted_records.append(record_to_dict(record))

    return inserted_records

from app.generic.external_data import t_booking_details_invoice,t_all
# import json

def safe_vector(vec):
    if vec is None:
        return None
    return [float(x) for x in list(vec)]

def t_record_to_dict(record: CustomerRecord) -> dict:
    return {
        "id": record.id,
        "booking_id": record.booking_id,
        "booking_json": record.booking_json,
        "emails_json": record.emails_json,
        "whatsapp_json": record.whatsapp_json,
        "call_logs_json": record.call_logs_json,
        "booking_status": record.booking_status,
        "primary_contact": record.primary_contact,
        "primary_email": record.primary_email,
        "prop_id": record.prop_id,
        "prop_name": record.prop_name,
        "travel_from_date": str(record.travel_from_date) if record.travel_from_date else None,
        "travel_to_date": str(record.travel_to_date) if record.travel_to_date else None,
        "updated_at": str(record.updated_at) if record.updated_at else None,
        "booking_vector": safe_vector(record.booking_vector),
        "email_vector": safe_vector(record.email_vector),
        "whatsapp_vector": safe_vector(record.whatsapp_vector),
        "calls_vector": safe_vector(record.calls_vector),
        "created_at": str(record.created_at) if record.created_at else None,
    }


def t_record_to_dicts_all(record: CustomerRecord) -> dict:
    
        
    """Convert a SQLAlchemy CustomerRecord into a JSON-safe dict."""
    return {
        "id": record.id,
        "booking_id": record.booking_id,
        "booking_json": record.booking_json,
        "emails_json": record.emails_json,
        "whatsapp_json": record.whatsapp_json,
        "call_logs_json": record.call_logs_json,
        "booking_status": record.booking_status,
        "primary_contact": record.primary_contact,
        "primary_email": record.primary_email,
        "prop_id": record.prop_id,
        "prop_name": record.prop_name,
        "travel_from_date": str(record.travel_from_date) if record.travel_from_date else None,
        "travel_to_date": str(record.travel_to_date) if record.travel_to_date else None,
        "updated_at": str(record.updated_at) if record.updated_at else None,
        "booking_vector": safe_vector(record.booking_vector),
        "email_vector": safe_vector(record.email_vector),
        "whatsapp_vector": safe_vector(record.whatsapp_vector),
        "calls_vector": safe_vector(record.calls_vector),
        "created_at": str(record.created_at) if record.created_at else None,
    }

async def t_app_booking_data(booking_ids: list[str]):
    """
    Fetch bookings + invoices from external API, generate embeddings, insert into PostgreSQL,
    return a JSON-safe list of inserted records.
    """
    inserted_records = []

    async with httpx.AsyncClient() as client:
        with SessionLocal() as db:  # type: Session
            for booking_id in booking_ids:
                # Fetch booking + invoice data
                details = await t_booking_details_invoice(booking_id)
                if not details:
                    continue

                booking_json = details.get("booking", [])
                invoices_json = details.get("invoices", [])

                # Use first booking as main reference
                bj = booking_json[0] if booking_json else {}

                # Optional: get emails/whatsapp/calls from your collector
                existing = await get_booking_details(booking_id)
                emails_json = existing.get("emails") if existing else []
                whatsapp_json = existing.get("whatsapp") if existing else []
                call_logs_json = existing.get("call_logs") if existing else []

                # Convert to text for embeddings
                booking_text = json.dumps({"booking": booking_json, "invoices": invoices_json}, default=json_converter, ensure_ascii=False)
                emails_text = json.dumps(emails_json, default=json_converter, ensure_ascii=False)
                whatsapp_text = json.dumps(whatsapp_json, default=json_converter, ensure_ascii=False)
                calls_text = json.dumps(call_logs_json, default=json_converter, ensure_ascii=False)

                # Generate embeddings
                booking_emb = generate_embedding(booking_text)
                email_emb = generate_embedding(emails_text)
                whatsapp_emb = generate_embedding(whatsapp_text)
                calls_emb = generate_embedding(calls_text)

                # Build record
                record = CustomerRecord(
                    booking_id=booking_id,
                    booking_json={"booking": booking_json, "invoices": invoices_json},
                    emails_json=emails_json,
                    whatsapp_json=whatsapp_json,
                    call_logs_json=call_logs_json,
                    booking_status=bj.get("booking_status"),
                    primary_contact=bj.get("traveller_contact_num"),
                    primary_email=bj.get("contact_email"),
                    prop_id=bj.get("prop_id"),
                    prop_name=bj.get("prop_name"),
                    travel_from_date=bj.get("travel_from_date"),
                    travel_to_date=bj.get("travel_to_date"),
                    updated_at=bj.get("booking_datetime"),
                    booking_vector=booking_emb,
                    email_vector=email_emb,
                    whatsapp_vector=whatsapp_emb,
                    calls_vector=calls_emb
                )

                db.add(record)
                db.commit()
                db.refresh(record)

                inserted_records.append(t_record_to_dict(record))

    return inserted_records
    
import asyncio
import httpx
from sqlalchemy.orm import Session


async def t_app_booking_data_all():
    """Insert all bookings returned by t_all()."""
    inserted_records = []
    all_bookings = await t_all()
    with SessionLocal() as db:
        for combined in all_bookings:
            bj = combined.get("booking", {})
            booking_id = bj.get("booking_id")
            if not booking_id:
                continue

            invoices_json = combined.get("invoice", [])
            communication = combined.get("communication", [])
            tickets = combined.get("tickets", [])
            calls_log = combined.get("calls", [])
            insert_booking_with_related(db, bj, invoices_json, calls_log, tickets, communication)

    return "inserted "

from sqlalchemy.orm import Session
from app.model.all_external_data import Booking, Invoice, CallLogs, Ticket, Communication

def insert_booking_with_related(
    db: Session,
    booking_data: dict,
    invoices_data: list[dict],
    calls_data: list[dict] = None,
    tickets_data: list[dict] = None,
    communications_data: list[dict] = None
):
    """Insert a booking and all its related records with error handling."""
    try:
        # Check if booking already exists
        existing_booking = db.query(Booking).filter(Booking.booking_id == booking_data["booking_id"]).first()
        if existing_booking:
            # Update existing booking or skip
            # For now, let's skip duplicates
            return existing_booking

        # Create booking record
        booking = Booking(
            booking_id=booking_data["booking_id"],
            booking_status=booking_data.get("booking_status"),
            primary_contact=booking_data.get("traveller_contact_num"),
            primary_email=booking_data.get("contact_email"),
            prop_id=booking_data.get("prop_id"),
            prop_name=booking_data.get("prop_name"),
            travel_from_date=booking_data.get("travel_from_date"),
            travel_to_date=booking_data.get("travel_to_date"),
            updated_at=booking_data.get("booking_datetime"),
            
            # Additional fields
            user_id=booking_data.get("user_id"),
            booking_type=booking_data.get("booking_type"),
            num_guests=booking_data.get("num_guests"),
            nights=booking_data.get("nights"),
            total_amount=booking_data.get("total_amount"),
            amount_paid=booking_data.get("amount_paid"),
            advance_amount=booking_data.get("advance_amount"),
            paid_advanced_amount=booking_data.get("paid_advanced_amount"),
            booking_datetime=booking_data.get("booking_datetime"),
            traveller_name=booking_data.get("traveller_name"),
            
            # JSON data
            booking_json=booking_data
        )

        db.add(booking)
        db.flush()  # so booking.id is available

        # Add invoices
        for inv in invoices_data:
            invoice = Invoice(
                booking_id=booking_data["booking_id"],
                invoice_id=inv.get("invoice_id"),
                invoice_type=inv.get("invoice_type"),
                invoice_from=inv.get("invoice_from"),
                invoice_to=inv.get("invoice_to"),
                received=inv.get("received"),
                payable=inv.get("payable"),
                receipt_status=inv.get("receipt_status"),
                duration=inv.get("duration"),
                invoice_status=inv.get("invoice_status"),
                invoice_json=inv
            )
            db.add(invoice)

        # Add calls
        if calls_data:
            for c in calls_data:
                call = CallLogs(
                    booking_id=booking_data["booking_id"],
                    username=c.get("username"),
                    ph_num=c.get("phNum"),
                    call_date=c.get("callDate"),
                    call_duration=c.get("callDuration"),
                    sales_phone_number=c.get("salesPhoneNumber"),
                    call_type=c.get("callType"),
                    call_json=c
                )
                db.add(call)

        # Add tickets
        if tickets_data:
            for t in tickets_data:
                ticket = Ticket(
                    booking_id=booking_data["booking_id"],
                    ticket_id=t.get("ticket_id"),
                    ticket_date=t.get("ticket_date"),
                    category=t.get("Category"),
                    description=t.get("description"),
                    prop_name=t.get("prop_name"),
                    status=t.get("status"),
                    assign_to=t.get("assign_to"),
                    resolved_by=t.get("resolved_by"),
                    ticket_json=t
                )
                db.add(ticket)

        # Add communications
        if communications_data:
            for comm in communications_data:
                communication = Communication(
                    booking_id=booking_data["booking_id"],
                    type=comm.get("type"),
                    comment=comm.get("comment"),
                    added_by=comm.get("added_by"),
                    timestamp=comm.get("timestamp"),
                    communication_json=comm
                )
                db.add(communication)

        db.commit()
        db.refresh(booking)
        return booking
        
    except Exception as e:
        db.rollback()
        raise e