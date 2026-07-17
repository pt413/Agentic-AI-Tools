import os
import time
import logging
import re
from datetime import datetime
from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status, Security
from fastapi.security import APIKeyHeader

from app.services.conversation_service import ConversationService
from app.db.database import get_db
from app.utils.metrics import (
    grpc_get_conversation_duration_ms,
    grpc_get_conversation_failures_total,
    grpc_get_conversation_requests_total,
)
from app.utils.logger import get_logger

log = get_logger("user_data_activity_route", level=logging.INFO)

STATIC_API_TOKEN = "hardcoded_secret_token_123"

api_key_header = APIKeyHeader(name="Authorization", auto_error=False)


def verify_static_token(authorization: str = Security(api_key_header)):
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization.replace("Bearer ", "").strip()

    if token != STATIC_API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def normalize_identifier(identifier: str | None) -> str | None:
    """
    Normalize phone numbers while leaving emails unchanged.
    """
    if not identifier:
        return identifier

    identifier = identifier.strip()

    # if email → return as is
    if "@" in identifier:
        return identifier.lower()

    # remove non-digit characters
    digits = re.sub(r"\D", "", identifier)

    # convert to last 10 digits (handles +91, 91 etc.)
    if len(digits) > 10:
        digits = digits[-10:]

    return digits


router = APIRouter(
    prefix="/activities",
    tags=["Unified Activities"],
    dependencies=[
        Security(api_key_header),
        Depends(verify_static_token),
    ],
)


@router.get("/conversation")
def get_conversation(
    id_1: str = Query(...),
    id_2: str = Query(...),
    channels: List[Literal["email", "whatsapp", "call"]] = Query(...),
    direction: Literal["sent", "received", "any"] = Query("any"),
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
    order: Literal["asc", "desc"] = Query("asc"),
    db=Depends(get_db),
):
    grpc_get_conversation_requests_total.inc()
    start = time.perf_counter()

    # normalize identifiers
    id_1 = normalize_identifier(id_1)
    id_2 = normalize_identifier(id_2)

    try:
        service = ConversationService(db)

        rows = service.get_conversation(
            id_1=id_1,
            id_2=id_2,
            channels=channels,
            direction=direction,
            from_date=from_date,
            to_date=to_date,
            order=order,
        )

    except Exception as e:
        duration_ms = (time.perf_counter() - start) * 1000
        grpc_get_conversation_duration_ms.observe(duration_ms)
        grpc_get_conversation_failures_total.inc()

        log.error(
            "get_conversation_failed",
            extra={"duration_ms": round(duration_ms, 2), "error": str(e)},
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"GetConversation failed: {str(e)}",
        )

    duration_ms = (time.perf_counter() - start) * 1000
    grpc_get_conversation_duration_ms.observe(duration_ms)

    log.info(
        "get_conversation_success",
        extra={"duration_ms": round(duration_ms, 2), "count": len(rows)},
    )

    return {
        "count": len(rows),
        "activities": [
            {
                "activity_id": row["activity_id"],
                "channel": row["channel"],
                "sender": row["sender"],
                "receiver": row["receiver"],
                "content": row["content"],
                "timestamp": row["timestamp"],
            }
            for row in rows
        ],
    }


@router.get("/fetch")
def fetch_user_activity(
    identifier: str = Query(...),
    channels: List[Literal["email", "whatsapp", "call"]] = Query(...),
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
    db=Depends(get_db),
):
    service = ConversationService(db)
    if "call" in channels or "whatsapp" in channels:
        identifier=identifier[-10:]

    # normalize identifier
    identifier = normalize_identifier(identifier)

    rows = service.get_user_activity(
        identifier=identifier,
        channels=channels,
        from_date=from_date,
        to_date=to_date,
    )

    return {
        "count": len(rows),
        "activities": rows,
    }