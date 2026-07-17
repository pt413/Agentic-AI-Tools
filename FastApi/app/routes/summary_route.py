# app/routes/summary_route.py
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from typing import Optional, List
from datetime import datetime
import os
import traceback

from app.db.database import SessionLocal
from app.model.summary import Summary
from app.model.message import Message
from app.schemas.summary_schema import SummaryResponse, SummariesListResponse
from app.services.summary_service import summarize_text  # your gemini service
from app.utils.text_chunker import chunk_text

router = APIRouter(prefix="/api/summaries", tags=["summaries"])

# -----------------------------
# Dependency
# -----------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# -----------------------------
# Helper to create/update summary
# -----------------------------
def upsert_summary(admin_number, cx_number, msgs, instruction, db: Session):
    combined_text = "\n".join([msg.clean_content or "" for msg in msgs]).strip()
    if not combined_text:
        return None
    print(combined_text)
    # 🔹 Break large text into smaller chunks
    chunks = chunk_text(combined_text, max_chars=1000)

    # 🔹 Summarize each chunk
    chunk_summaries = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        chunk_summary = summarize_text(chunk, instruction)
        if chunk_summary:
            chunk_summaries.append(chunk_summary)

    if not chunk_summaries:
        return None  # nothing to summarize

    # 🔹 Combine all chunk summaries into final summary
    final_summary = summarize_text("\n".join(chunk_summaries), "Combine into one coherent summary.")

    # Check if summary exists already
    summary_record = (
        db.query(Summary)
        .filter(Summary.admin_number == admin_number, Summary.cx_number == cx_number)
        .first()
    )

    if summary_record:
        summary_record.wa_summary = final_summary
        summary_record.last_updated_on = datetime.utcnow()
    else:
        summary_record = Summary(
            admin_number=admin_number,
            cx_number=cx_number,
            wa_summary=final_summary,
            created_on=datetime.utcnow(),
            last_updated_on=datetime.utcnow(),
            created_by=admin_number,
        )
        db.add(summary_record)

    db.commit()
    db.refresh(summary_record)
    return summary_record

# -----------------------------
# Generate summaries
# -----------------------------
@router.post("/generate", response_model=SummariesListResponse)
def generate_summaries(
    admin_number: str = Query(..., description="Admin number"),
    cx_number: Optional[str] = Query(None, description="Optional customer number"),
    instruction: Optional[str] = Query(None, description="Optional instruction for Gemini"),
    db: Session = Depends(get_db)
):
    try:
        summaries = []

        if cx_number:
            # Case 1: Single customer under admin
            msgs = (
                db.query(Message)
                .filter(Message.admin_number == admin_number, Message.cx_number == cx_number)
                .order_by(Message.timestamp.asc())
                .all()
            )
            if not msgs:
                raise HTTPException(status_code=404, detail="No messages found for this customer")
            summary_record = upsert_summary(admin_number, cx_number, msgs, instruction, db)
            if summary_record:
                summaries.append(summary_record)

        else:
            # Case 2: All customers under admin
            messages = db.query(Message).filter(Message.admin_number == admin_number).all()
            if not messages:
                raise HTTPException(status_code=404, detail="No messages found for this admin")

            # Group by customer
            grouped = {}
            for msg in messages:
                grouped.setdefault(msg.cx_number, []).append(msg)

            for customer, msgs in grouped.items():
                summary_record = upsert_summary(admin_number, customer, msgs, instruction, db)
                if summary_record:
                    summaries.append(summary_record)

        return SummariesListResponse(success=True, count=len(summaries), data=summaries)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



# -----------------------------
# Get single summary
# -----------------------------
@router.get("/{summary_id}", response_model=SummaryResponse)
def get_summary(summary_id: int, db: Session = Depends(get_db)):
    summary = db.query(Summary).filter(Summary.id == summary_id).first()
    if not summary:
        raise HTTPException(status_code=404, detail="Summary not found")
    return summary


# -----------------------------
# Delete summary
# -----------------------------
@router.delete("/{summary_id}")
def delete_summary(summary_id: int, db: Session = Depends(get_db)):
    summary = db.query(Summary).filter(Summary.id == summary_id).first()
    if not summary:
        raise HTTPException(status_code=404, detail="Summary not found")
    db.delete(summary)
    db.commit()
    return {"success": True, "message": "Summary deleted"}
