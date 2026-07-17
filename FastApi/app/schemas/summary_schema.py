# app/schemas/summary_schema.py
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

# -----------------------------
# Base schema for Summary
# -----------------------------
class SummaryBase(BaseModel):
    admin_number: str
    cx_number: Optional[str] = None
    email_summary: Optional[str] = None
    wa_summary: Optional[str] = None
    call_summary: Optional[str] = None
    created_by: Optional[str] = None

# -----------------------------
# Response schema
# -----------------------------
class SummaryResponse(SummaryBase):
    id: int
    created_on: Optional[datetime] = None
    last_updated_on: Optional[datetime] = None

    class Config:
        from_attributes = True  # Allows SQLAlchemy model -> Pydantic conversion
        orm_mode = True

# -----------------------------
# List response
# -----------------------------
class SummariesListResponse(BaseModel):
    success: bool
    count: int
    data: list[SummaryResponse]
