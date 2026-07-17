from pydantic import BaseModel
from typing import Optional, Literal, List

class GeoResult(BaseModel):
    type: Literal["person", "property"]
    property_id: Optional[str] = None
    property_name: Optional[str] = None
    unit: Optional[str] = None
    bedrooms: Optional[str] = None
    daily_rent: Optional[float] = None
    building: Optional[str] = None
    address: Optional[str] = None
    direction: Optional[str] = None
    caretaker: Optional[str] = None
    name: Optional[str] = None
    phone: Optional[str] = None
    role: Optional[str] = None
    distance_m: float

class GeoNearestRequest(BaseModel):
    lat: float
    lng: float
    intent: Literal["property", "person"]
    role: Optional[str] = None
    limit: int = 1

class GeoNearestResponse(BaseModel):
    result: List[GeoResult]
