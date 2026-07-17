from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

class EmailSchema(BaseModel):
    subject: str
    direction: str
    sender: str
    receiver: str
    date: str
    body: str
    snippet: str
    msgid: str
    thread_id: str
    summary: str
    last_updated:Optional[datetime]
    embedding: List[float]
    subject_embedding: List[float]

    class Config:
        orm_mode = True
