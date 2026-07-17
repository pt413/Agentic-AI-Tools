from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime

class CustomerRecordBase(BaseModel):
    booking_id: str
    booking_json: Optional[Dict] = None
    emails_json: Optional[List[Dict]] = None
    whatsapp_json: Optional[List[Dict]] = None
    call_logs_json: Optional[List[Dict]] = None

    booking_status: Optional[str] = None
    primary_contact: Optional[str] = None
    primary_email: Optional[str] = None
    prop_id: Optional[str] = None
    prop_name: Optional[str] = None
    travel_from_date: Optional[datetime] = None
    travel_to_date: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # embeddings as lists of floats
    booking_vector: Optional[List[float]] = None
    email_vector: Optional[List[float]] = None
    whatsapp_vector: Optional[List[float]] = None
    calls_vector: Optional[List[float]] = None

class CustomerRecordCreate(CustomerRecordBase):
    pass

class CustomerRecordResponse(CustomerRecordBase):
    id: int
    created_at: datetime

    class Config:
        orm_mode = True
