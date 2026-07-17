from pydantic import BaseModel
from typing import List, Optional

class ScrapedPageCreate(BaseModel):
    url: str

class ScrapedPageResponse(BaseModel):
    id: int
    url: str
    content: str
    chunks: Optional[List[str]] = None
    embedding: Optional[List[List[float]]] = None

    class Config:
        from_attributes = True  # replaces orm_mode in Pydantic v2
