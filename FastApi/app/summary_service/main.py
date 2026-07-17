from fastapi import FastAPI, Query, Depends, HTTPException
from sqlalchemy.orm import Session
from source_fetcher import get_whatsapp_conversations
from db import schemas
from db.database import get_db
from summary_engine import summarize
import summary_crud
from datetime import datetime

app = FastAPI()

@app.post("/summarize/whatsapp", response_model=schemas.SummaryOut)
def summarize_whatsapp(
    cx_number: str = Query(..., description="Customer number for WhatsApp conversation"),
    day: str = Query(None, description="Date in YYYY-MM-DD format. Defaults to today if not provided."),
    db: Session = Depends(get_db)
):
    # If day is not provided, use today
    if not day:
        day = datetime.today().strftime("%Y-%m-%d")

    print(f"Fetching WhatsApp Conversations for {cx_number} on {day}...")
    convos_by_admin = get_whatsapp_conversations(cx_number, day)

    if not convos_by_admin:
        raise HTTPException(status_code=404, detail=f"No messages found for this customer on {day}")

    summaries = []
    for admin, convo in convos_by_admin.items():
        print(f"Summarizing conversation with admin {admin}...")
        summary = summarize(convo)
        summaries.append(summary)

    final_summary = " ".join(summaries)
    print("Got final summary")

    summary_data = schemas.SummaryCreate(
        source_type="whatsapp",
        source_id=f"{cx_number}_{day}",
        original_text="\n\n".join(convos_by_admin.values()),
        summary_text=final_summary,
    )
    return summary_crud.create_summary(db, summary_data)
