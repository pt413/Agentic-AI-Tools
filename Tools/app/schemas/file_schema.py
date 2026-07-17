from pydantic import BaseModel
from datetime import datetime
from typing import List, Optional, Any

class UploadedFileBase(BaseModel):
    filename: str
    content_type: str
    user_id: str

class UploadedFileCreate(UploadedFileBase):
    data: List[dict]

class UploadedFileResponse(UploadedFileBase):
    id: int
    uploaded_at: datetime
    data: Optional[List[Any]] = None

    class Config:
        from_attributes = True
