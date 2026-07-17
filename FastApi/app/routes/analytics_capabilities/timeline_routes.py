from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.routes.analytics_capabilities.common import (
    TimelineConversationRequest,
    TimelineFactsRequest,
    TimelineIdentityRequest,
    _api_error,
    _encode,
)
from app.services.analytics_engine.capabilities.timeline_access_service import TimelineAccessService


router = APIRouter()


@router.post("/timeline/identity")
def timeline_identity(payload: TimelineIdentityRequest, db: Session = Depends(get_db)) -> Any:
    try:
        service = TimelineAccessService(db=db, schema=payload.schema_name)
        return _encode(service.identity(**payload.seed_kwargs()))
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/timeline/facts")
def timeline_facts(payload: TimelineFactsRequest, db: Session = Depends(get_db)) -> Any:
    try:
        service = TimelineAccessService(db=db, schema=payload.schema_name)
        return _encode(service.facts(mode=payload.mode, entities=payload.entities, **payload.seed_kwargs()))
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/timeline/conversation")
def timeline_conversation(payload: TimelineConversationRequest, db: Session = Depends(get_db)) -> Any:
    try:
        service = TimelineAccessService(db=db, schema=payload.schema_name)
        return _encode(service.conversation(channel=payload.channel, days=payload.days, **payload.seed_kwargs()))
    except Exception as exc:
        raise _api_error(exc) from exc
