from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from app.services.lead_google_contacts.sync_service import sync_leads_to_google_contacts, delete_contact_by_lead_id

router = APIRouter(prefix="/api/lead_contact", tags=["Lead Contact"])

class Lead(BaseModel):
    lead_id: str
    full_name: str
    email_id: Optional[str] = None
    contact_number: Optional[str] = None

class DeleteLeadsRequest(BaseModel):
    lead_ids: List[str]

@router.post("/add-google-contacts")
def sync_google_contacts_api(leads: List[Lead]):
    try:
        if not leads:
            raise HTTPException(status_code=400, detail="No leads provided")
        leads_data = [lead.dict() for lead in leads]
        result = sync_leads_to_google_contacts(leads_data,"credentials/token_lead.pickle")
        return {
            "status": "success",
            "data": result
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@router.post("/delete-google-contacts", summary="Delete one or multiple Google Contacts")
def delete_google_contacts_api(request: DeleteLeadsRequest):
    lead_ids_to_delete = request.lead_ids or []

    if not lead_ids_to_delete:
        raise HTTPException(status_code=400, detail="No lead IDs provided")

    results = {"deleted": [], "not_found": []}

    for lid in lead_ids_to_delete:
        deleted = delete_contact_by_lead_id(lid)
        if deleted:
            results["deleted"].append(lid)
        else:
            results["not_found"].append(lid)

    return {"status": "success", "data": results}

@router.post("/add-google-contacts-whatsapp")
def sync_google_contacts_api_whatsapp(leads: List[Lead]):
    try:
        if not leads:
            raise HTTPException(status_code=400, detail="No leads provided")
        leads_data = [lead.dict() for lead in leads]
        result = sync_leads_to_google_contacts(leads_data,"credentials/token_whatsapp.pickle")
        return {
            "status": "success",
            "data": result
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))