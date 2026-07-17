from pydantic import BaseModel
from typing import Optional, List

class AdverseLookupRequest(BaseModel):
    phone: Optional[str] = None
    email: Optional[str] = None
    pan: Optional[str] = None
    aadhaar: Optional[str] = None