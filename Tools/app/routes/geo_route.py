from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.schemas.geo_schema import GeoNearestRequest, GeoNearestResponse
from app.services.geo_service import GeoService

router = APIRouter(prefix="/geo", tags=["Geo Intelligence"])

@router.post("/nearest", response_model=GeoNearestResponse)
def geo_nearest(
    payload: GeoNearestRequest,
    db: Session = Depends(get_db)
):
    service = GeoService(db)
    if payload.intent == "person":
        role = payload.role.strip() if payload.role else None
        if role == "":
            role = None
        result = service.nearest_person(
            lat=payload.lat,
            lng=payload.lng,
            role=role,
            limit=payload.limit
        )
        return {"result": result}
    elif payload.intent == "property":
        result = service.nearest_property(
            lat=payload.lat,
            lng=payload.lng,
            limit=payload.limit
        )
        return {"result": result}
    raise HTTPException(status_code=400, detail="Invalid intent")
