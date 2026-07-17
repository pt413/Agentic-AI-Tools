from app.services.lead_google_contacts.google_client import get_google_service
from app.db.database import SessionLocal
from sqlalchemy import text
import logging
import re

logger = logging.getLogger(__name__)

def normalize_phone(phone):
    phone = phone.strip()
    if phone.startswith("91") and len(phone) == 12:
        phone = "+" + phone
    if not phone.startswith("+"):
        phone = "+91" + phone[-10:]
    return phone

def normalize_name(email):
    if not email:
        return "Unknown"
    name = email.split("@")[0]
    name = re.sub(r"[._\-]+", " ", name)
    name = re.sub(r"\d+", "", name)
    name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
    return name.strip().title()

def create_contact(service, name, email, phone, lead_id):
    body = {
        "names": [{
            "givenName": f"{name} (Lead {lead_id})"
        }],
        "phoneNumbers": [{
            "value": phone
        }],
        "biographies": [{
            "value": f"LEAD_ID:{lead_id}"
        }]
    }
    if email:
        body["emailAddresses"] = [{"value": email}]
    result = service.people().createContact(body=body).execute()
    return result.get("resourceName")

def sync_leads_to_google_contacts(leads: list, token_path: str):
    service = get_google_service(token_path)
    db = SessionLocal()
    results = {
        "created": [],
        "skipped_existing": [],
        "errors": []
    }
    try:
        for lead in leads:
            try:
                lead_id = lead.get("lead_id")
                email = lead.get("email_id")
                # name = lead.get("full_name")
                if(name==None or name=="string"):
                    name=""
                else:
                    name=lead.get("full_name")
                phone = lead.get("contact_number")
                if not phone:
                    logger.warning(f"Skipping lead {lead_id}: no phone")
                    continue
                phone = normalize_phone(phone)
                name = name
                if email=="string":
                    email=""
                else:
                    email=email
                logger.info(f"Processing lead {lead_id}")
                existing = db.execute(
                    text("""
                        SELECT google_resource_name
                        FROM public.google_contact_mapping
                        WHERE lead_id = :lead_id
                    """),
                    {"lead_id": lead_id}
                ).fetchone()
                if existing:
                    results["skipped_existing"].append(lead_id)
                    continue
                contact_id = create_contact(service, name, email, phone, lead_id)
                db.execute(
                    text("""
                        INSERT INTO public.google_contact_mapping 
                        (lead_id, name, email, phone, google_resource_name)
                        VALUES (:lead_id, :name, :email, :phone, :contact_id)
                        ON CONFLICT (lead_id) DO NOTHING
                    """),
                    {
                        "lead_id": lead_id,
                        "name": name,
                        "email": email,
                        "phone": phone,
                        "contact_id": contact_id
                    }
                )
                db.commit()
                results["created"].append({
                    "lead_id": lead_id,
                    "name": name,
                    "contact_id": contact_id
                })
            except Exception as e:
                db.rollback()
                logger.exception(f"[Google Sync Error] Lead {lead_id}: {e}")
                results["errors"].append({
                    "lead_id": lead_id,
                    "error": str(e)
                })
    finally:
        db.close()
    return results

def delete_contact_by_lead_id(lead_id: str):
    service = get_google_service()
    db = SessionLocal()
    try:
        result = db.execute(
            text("""
                SELECT google_resource_name
                FROM public.google_contact_mapping
                WHERE lead_id = :lead_id
            """),
            {"lead_id": lead_id}
        ).fetchone()
        contact_id = result[0] if result else None
        logger.info(f"Deleting lead {lead_id}, contact_id: {contact_id}")
        if not contact_id:
            return False
        service.people().deleteContact(resourceName=contact_id).execute()
        db.execute(
            text("""
                DELETE FROM public.google_contact_mapping
                WHERE lead_id = :lead_id
            """),
            {"lead_id": lead_id}
        )
        db.commit()
        return True
    except Exception as e:
        logger.exception(f"[Google Delete Error] Lead {lead_id}: {e}")
        return False
    finally:
        db.close()