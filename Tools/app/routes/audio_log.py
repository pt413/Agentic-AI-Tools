from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import cast, func, TIMESTAMP,case
from app.model.audio_file_model import AudioFile
from app.db.database import get_db

from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
# import traceback

router = APIRouter(prefix="/api/audio_details", tags=["Audio_Details"])

@router.get("/")
def get_audio_summary(db: Session = Depends(get_db)):
    results = (
        db.query(
            AudioFile.emp_phone_number.label("emp_phone_number"),
            func.count(func.distinct(AudioFile.customer_phone_number)).label("total_calls"),
            func.min(cast(AudioFile.call_datetime, TIMESTAMP)).label("from_date"),
            func.max(cast(AudioFile.call_datetime, TIMESTAMP)).label("to_date"),
            # func.max(cast(AudioFile.uploaded_at, TIMESTAMP)).label("last_sync"),
            func.max(
            case(
            (
            (AudioFile.transcript_text != None) & 
            (AudioFile.transcript_text != ""),
            cast(AudioFile.uploaded_at, TIMESTAMP)
            ),
            else_=None)).label("last_sync"),
            func.sum((case((AudioFile.call_type=="Missed",1),else_=0))).label("missed"),
            func.sum(case((AudioFile.call_type=="incoming",1),else_=0)).label("incoming"),
            func.sum(case((AudioFile.call_type=="outgoing",1),else_=0)).label("outgoing"),
            func.max(AudioFile.call_duration).label("duration"),
            func.max(AudioFile.transcript_text).label("transcript_text")
        )
        .group_by(AudioFile.emp_phone_number)
        .all()
    )

    data = [
        {
            "Sales_Number": r.emp_phone_number,
            "Total_Calls": r.total_calls,
            "From_date": r.from_date.strftime("%Y-%m-%d %H:%M:%S") if r.from_date else None,
            "To_date": r.to_date.strftime("%Y-%m-%d %H:%M:%S") if r.to_date else None,
            "Last_sync": 
            # (
            #         r.last_sync.replace(tzinfo=ZoneInfo("UTC")).astimezone(IST).isoformat() 
            #         if r.last_sync else None
            #         ),
            r.last_sync.strftime("%Y-%m-%d %H:%M:%S") if r.last_sync else None,
            "Missed": r.missed,
            "Incoming":r.incoming,
            "Outgoing":r.outgoing,
            "Duration":r.duration,
            "Transcript":r.transcript_text
        }
        for r in results
    ]

    return data
