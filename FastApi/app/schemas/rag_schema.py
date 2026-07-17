from pydantic import BaseModel
from typing import Optional, Any

class RagSearchRequest(BaseModel):
    query: str
    top_k: int = 5
    source: str | None = None
    rag_mode: Optional[str] = None

class EphemeralRag(BaseModel):
    query: str
    top_k: int = 5
    data: Optional[Any] = None
