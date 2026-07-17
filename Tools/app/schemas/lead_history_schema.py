from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class LeadHistoryBase(BaseModel):
    referal_page: Optional[str] = None
    current_page: Optional[str] = None
    ip_address: Optional[str] = None
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    lead_id: Optional[str] = None
    prop_id: Optional[str] = None
    timestamp: Optional[datetime] = None
    source: Optional[str] = None
    user_agent: Optional[str] = None
    # mobile_no: Optional[str] = None
    # email_id: Optional[str] = None


class LeadHistoryCreate(LeadHistoryBase):
    pass


class LeadHistoryResponse(LeadHistoryBase):
    id: int

    class Config:
        orm_mode = True

