from pydantic import BaseModel
from datetime import datetime

class SummaryBase(BaseModel):
    source_type: str
    source_id: str
    original_text: str

class SummaryCreate(SummaryBase):
    summary_text: str

class SummaryOut(BaseModel):
    id: int
    source_type: str
    source_id: str
    original_text: str
    summary_text: str
    created_at: datetime

    class Config:
        orm_mode = True
