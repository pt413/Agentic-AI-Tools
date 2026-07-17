from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.services.analytics_engine.capabilities.timeline_access_service import TimelineAccessService


router = APIRouter(prefix="/analytics_service", tags=["analytics_service"])


def _service(db: Session) -> TimelineAccessService:
    return TimelineAccessService(db=db)


def _validate_single_seed(
    *,
    user_id: Optional[int],
    booking_id: Optional[int],
    lead_id: Optional[int],
    person_id: Optional[int],
    email: Optional[str],
    phone: Optional[str],
) -> None:
    provided = {
        "user_id": user_id,
        "booking_id": booking_id,
        "lead_id": lead_id,
        "person_id": person_id,
        "email": email,
        "phone": phone,
    }

    active = {k: v for k, v in provided.items() if v not in (None, "")}

    if not active:
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of user_id, booking_id, lead_id, person_id, email, or phone.",
        )

    if len(active) != 1:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Multiple identifiers were provided. Please select exactly one and retry.",
                "provided_identifiers": active,
                "action": "Select one identifier and retry the request.",
            },
        )

from dataclasses import dataclass
from typing import Optional, Dict, Any


@dataclass
class MultipleSeedsError(Exception):
    message: str
    candidates: Dict[str, Any]
    
@router.get("/identity")
def get_identity(
    user_id: Optional[int] = Query(default=None),
    booking_id: Optional[int] = Query(default=None),
    lead_id: Optional[int] = Query(default=None),
    person_id: Optional[int] = Query(default=None),
    email: Optional[str] = Query(default=None),
    phone: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    _validate_single_seed(
        user_id=user_id,
        booking_id=booking_id,
        lead_id=lead_id,
        person_id=person_id,
        email=email,
        phone=phone,
    )
    try:
        return _service(db).identity(
            user_id=user_id,
            booking_id=booking_id,
            lead_id=lead_id,
            person_id=person_id,
            email=email,
            phone=phone,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"identity lookup failed: {exc}") from exc


from fastapi import HTTPException

@router.get("/facts")
def get_facts(
    mode: str = Query(default="active", pattern="^(active|all)$"),
    entities: str = Query(default="all"),
    user_id: Optional[int] = Query(default=None),
    booking_id: Optional[int] = Query(default=None),
    lead_id: Optional[int] = Query(default=None),
    person_id: Optional[int] = Query(default=None),
    email: Optional[str] = Query(default=None),
    phone: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    try:
        _validate_single_seed(
            user_id=user_id,
            booking_id=booking_id,
            lead_id=lead_id,
            person_id=person_id,
            email=email,
            phone=phone,
        )

        return _service(db).facts(
            mode=mode,
            entities=entities,
            user_id=user_id,
            booking_id=booking_id,
            lead_id=lead_id,
            person_id=person_id,
            email=email,
            phone=phone,
        )

    except MultipleSeedsError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                # "message": exc.message,
                "provided_identifiers": exc.candidates,
                # "action": "Select one identifier and retry the request.",
            },
        ) from exc

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"facts lookup failed: {exc}") from exc

@router.get("/conversation")
def get_conversation(
    channel: str = Query(default="any", pattern="^(any|whatsapp|email)$"),
    days: int = Query(default=7, ge=1, le=365),
    user_id: Optional[int] = Query(default=None),
    booking_id: Optional[int] = Query(default=None),
    lead_id: Optional[int] = Query(default=None),
    person_id: Optional[int] = Query(default=None),
    email: Optional[str] = Query(default=None),
    phone: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    _validate_single_seed(
        user_id=user_id,
        booking_id=booking_id,
        lead_id=lead_id,
        person_id=person_id,
        email=email,
        phone=phone,
    )
    try:
        return _service(db).conversation(
            channel=channel,
            days=days,
            user_id=user_id,
            booking_id=booking_id,
            lead_id=lead_id,
            person_id=person_id,
            email=email,
            phone=phone,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"conversation lookup failed: {exc}") from exc
