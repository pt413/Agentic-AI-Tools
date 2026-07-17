from pydantic import BaseModel
from typing import Optional, Dict
from datetime import datetime

class OrganizationBase(BaseModel):
    name: str
    domain: Optional[str] = None
    hierarchy: Optional[Dict] = {}
    details: Optional[Dict] = {}

class OrganizationCreate(OrganizationBase):
    pass

class OrganizationResponse(OrganizationBase):
    id: int
    created_at: datetime

    class Config:
        orm_mode = True
