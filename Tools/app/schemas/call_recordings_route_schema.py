

from pydantic import BaseModel, Field, validator
from datetime import datetime
from typing import Optional, Union


# ==================== AUDIO FILE SCHEMAS ====================

class AudioFileCreate(BaseModel):
    id: int
    call_id: Optional[str] = None
    emp_phone_number: Optional[str] = None
    emp_name: Optional[str] = None
    customer_phone_number: Optional[str] = None
    call_datetime: Optional[datetime] = None
    call_duration: Optional[int] = None
    call_type: Optional[str] = None
    department: Optional[str] = None
    audio_url: Optional[str] = None
    filename: Optional[str] = None
    transcript_text: Optional[str] = None
    uploaded_at: Optional[datetime] = None
    status: Optional[int] = 0


class AudioFileOut(BaseModel):
    id: int
    call_id: Optional[str] = None
    emp_phone_number: Optional[str] = None
    emp_name: Optional[str] = None
    customer_phone_number: Optional[str] = None
    call_datetime: Optional[datetime] = None
    call_duration: Optional[int] = None
    call_type: Optional[str] = None
    department: Optional[str] = None
    audio_url: Optional[str] = None
    filename: Optional[str] = None
    transcript_text: Optional[str] = None
    uploaded_at: Optional[datetime] = None
    status: Optional[int] = 0

    class Config:
        from_attributes = True


class CallLogCreate(BaseModel):
    """Schema for creating call logs - matches Kotlin field names"""
    sales_number: str
    call_id: str
    name: Optional[str] = None
    number: str  # ✅ Changed from customer_phone
    date_ms: Union[int, str]  # ✅ Changed from call_timestamp
    duration_sec: Union[int, str] = 0
    type: str  # ✅ Changed from call_type
    acc_id: Optional[str] = None
    file_url: Optional[str] = ""
    filename: Optional[str] = ""
    
    @validator('date_ms', pre=True)
    def validate_timestamp(cls, v):
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                raise ValueError("date_ms must be a valid integer")
        return v
    
    @validator('duration_sec', pre=True)
    def validate_duration(cls, v):
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                return 0
        return v if v is not None else 0
    
    class Config:
        json_schema_extra = {
            "example": {
                "sales_number": "9164729897",
                "call_id": "6602",
                "name": "John Doe",
                "number": "918088044937",
                "date_ms": "1731372103511",
                "duration_sec": "45",
                "type": "outgoing",
                "acc_id": "Finance",
                "file_url": "",
                "filename": ""
            }
        }


class CallLogResponse(BaseModel):
    """Standard response schema for /save-call-log endpoint"""
    message: str
    inserted_id: Optional[int] = None
    existing_id: Optional[int] = None
    status: str = Field(..., description="'created', 'duplicate', or 'updated'")
    has_recording: bool = False
    updated: Optional[bool] = False


class CallLogOut(BaseModel):
    """Schema for call log output response"""
    id: int
    call_id: Optional[str] = None
    sales_phone_number: Optional[str] = None
    username: Optional[str] = None
    ph_num: Optional[str] = None
    call_date: Optional[datetime] = None
    call_duration: Optional[int] = None
    call_type: Optional[str] = None
    admin_Team: Optional[str] = None
    file_url: Optional[str] = None
    filename: Optional[str] = None
    transcribed_text: Optional[str] = None
    uploaded_at: Optional[datetime] = None
    status: Optional[int] = 0  # ✅ NEW FIELD

    class Config:
        from_attributes = True

# ==================== LOCATION SCHEMAS ====================

class LocationCreate(BaseModel):
    """Schema for creating a location record"""
    sales_number: str
    address: str
    timestamp: Optional[int] = None
    # status: Optional[int] = 0  # ✅ NEW FIELD

class LocationOut(BaseModel):
    """Schema for location output response"""
    id: int
    sales_phone_number: str
    location: str
    time: Optional[datetime] = None
    # status: Optional[int] = 0  # ✅ NEW FIELD
    class Config:
        from_attributes = True


# ==================== SALES DATA SCHEMAS ====================


class SalesDataOut(BaseModel):
    """Schema for sales data output"""
    id: int
    username: str
    salesPhoneNumber: str
    admin_Team: Optional[str] = None
    status: Optional[int] = 0  # ✅ NEW FIELD

    class Config:
        from_attributes = True







# class AudioFileCreate(BaseModel):
#     """Schema for creating a new audio file record"""

#     id: int
#     call_id: Optional[str] = None
#     sales_phone_number: Optional[str] = None
#     username: Optional[str] = None
#     ph_num: Optional[str] = None
#     call_date: Optional[datetime] = None
#     call_duration: Optional[int] = None
#     call_type: Optional[str] = None
#     admin_Team: Optional[str] = None
#     file_url: Optional[str] = None
#     filename: Optional[str] = None
#     transcribed_text: Optional[str] = None
#     uploaded_at: Optional[datetime] = None
#     status: Optional[int] = 0  # ✅ NEW FIELD





# class AudioFileOut(BaseModel):
#     """Schema for audio file output response"""
#     id: int
#     call_id: Optional[str] = None
#     sales_phone_number: Optional[str] = None
#     username: Optional[str] = None
#     ph_num: Optional[str] = None
#     call_date: Optional[datetime] = None
#     call_duration: Optional[int] = None
#     call_type: Optional[str] = None
#     admin_Team: Optional[str] = None
#     file_url: Optional[str] = None
#     filename: Optional[str] = None
#     transcribed_text: Optional[str] = None
#     uploaded_at: Optional[datetime] = None
#     status: Optional[int] = 0  # ✅ NEW FIELD

#     class Config:
#         from_attributes = True