# app/schemas/call_logs_schema.py
from datetime import datetime
from pydantic import BaseModel, Field
from typing import Optional, List

class CallLogSchema(BaseModel):
    username: Optional[str] = None           # Admin name (merged)
    phNum: Optional[str] = None              # Customer phone number
    salesPhoneNumber: Optional[str] = None   # Admin's phone number
    callType: Optional[str] = None
    callDuration: Optional[int] = None
    lead_id: Optional[str] = None
    timestamp: Optional[int] = None
    callDate: Optional[datetime] = None
    added_on: Optional[datetime] = None
    admin_Team: Optional[str] = None         # Admin team (merged)

    class Config:
        orm_mode = True


class MakeCallRequest(BaseModel):
    phone_number: str = Field(..., description="The phone number to call")
    user_id: str = Field(..., description="The ID of the user making the call")
    lead_id: Optional[str] = Field(None, description="The ID of the lead associated with the call")


class MakeCallResponse(BaseModel):
    success: bool
    message: str
    callId: str
    timestamp: datetime


class LLMRequest(BaseModel):
    callType: str = Field(..., description="Type of call (Incoming, Outgoing, Missed)")
    lead_id: Optional[str] = Field(None, description="The ID of the lead")
    phone_number: Optional[str] = Field(None, description="The phone number involved")
    call_duration: Optional[int] = Field(None, description="Duration of the call in seconds")


class LLMSuggestionResponse(BaseModel):
    suggestion: str
    nextSteps: List[str]


class CallNotesRequest(BaseModel):
    call_id: str = Field(..., description="The ID of the call")
    notes: str = Field(..., description="The notes to save")
    user_id: str = Field(..., description="The ID of the user saving the notes")


class CallNotesResponse(BaseModel):
    success: bool
    message: str
    savedAt: datetime


class HealthResponse(BaseModel):
    status: str
    timestamp: datetime
    callLogsCount: int
    dataSource: str
