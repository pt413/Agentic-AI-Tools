from pydantic import BaseModel
from datetime import datetime
from typing import Optional

class WebPageBase(BaseModel):
    url: str

class WebPageCreate(WebPageBase):
    content: str

class WebPageResponse(WebPageBase):
    id: int
    content: str
    scraped_at: datetime

    class Config:
        from_attributes = True
