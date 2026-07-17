from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, case
from sqlalchemy.dialects.postgresql import JSON
from app.db.database import get_db
from app.model.audio_file_model import AudioFile as cl

from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

router = APIRouter(prefix="/api/call_log", tags=["Call_Log"])


@router.get("/")
def get_call_log_summary(db: Session = Depends(get_db)):

    try:
        adm_team = (
            db.query(
                cl.emp_phone_number.label("emp_phone_number"),
                cl.department.label("admin_team"),
                func.count(cl.id).label("team_count"),
            )
            .group_by(cl.emp_phone_number, cl.department)
            .subquery()
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error in admin team query: {str(e)}",
        )

    try:
        adm_json = (
            db.query(
                adm_team.c.emp_phone_number,
                func.json_object_agg(
                    adm_team.c.admin_team,
                    adm_team.c.team_count,
                ).label("admin_team_counts"),
            )
            .group_by(adm_team.c.emp_phone_number)
            .subquery()
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error in admin team aggregation: {str(e)}",
        )

    try:
        summary_subq = (
            db.query(
                cl.emp_phone_number.label("emp_phone_number"),

                func.count(cl.id).label("total_calls"),

                # duration > 0
                func.sum(
                    case(
                        (cl.call_duration > 0, 1),
                        else_=0,
                    )
                ).label("connected_calls"),

                # translated_text is null AND audio_url exists
                func.sum(
                    case(
                        (
                            (cl.translated_text == None) &
                            (cl.audio_url != None),
                            1,
                        ),
                        else_=0,
                    )
                ).label("remaining_transcript"),

                # audio_url is null
                func.sum(
                    case(
                        (cl.audio_url == None, 0),
                        else_=1,
                    )
                ).label("audio_urls"),

                func.min(cl.call_datetime).label("from_date"),
                func.max(cl.call_datetime).label("to_date"),
                func.max(cl.uploaded_at).label("last_sync"),

                func.sum(
                    case(
                        (cl.call_type == "missed", 1),
                        else_=0,
                    )
                ).label("missed_calls"),

                func.sum(
                    case(
                        (cl.call_type == "incoming", 1),
                        else_=0,
                    )
                ).label("received_calls"),

                func.sum(
                    case(
                        (cl.call_type == "outgoing", 1),
                        else_=0,
                    )
                ).label("outgoing_calls"),

                func.max(cl.call_duration).label("total_duration"),
            )
            .group_by(cl.emp_phone_number)
            .subquery()
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error in summary query: {str(e)}",
        )

    try:
        results = (
            db.query(
                summary_subq.c.emp_phone_number,
                summary_subq.c.total_calls,
                summary_subq.c.connected_calls,
                summary_subq.c.remaining_transcript,
                summary_subq.c.audio_urls,
                summary_subq.c.from_date,
                summary_subq.c.to_date,
                summary_subq.c.last_sync,
                summary_subq.c.missed_calls,
                summary_subq.c.received_calls,
                summary_subq.c.outgoing_calls,
                summary_subq.c.total_duration,
                func.coalesce(
                    adm_json.c.admin_team_counts,
                    func.cast("{}", JSON),
                ).label("admin_team_counts"),
            )
            .outerjoin(
                adm_json,
                summary_subq.c.emp_phone_number == adm_json.c.emp_phone_number,
            )
            .all()
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error in final data aggregation: {str(e)}",
        )

    data = []

    try:
        for r in results:
            data.append(
                {
                    "Sales_Phone_Number": r.emp_phone_number,
                    "Total_Calls": r.total_calls,

                    "Connected_calls": r.connected_calls or 0,
                    "Remaining_Transcript": r.remaining_transcript or 0,
                    "Audio_urls": r.audio_urls or 0,

                    "From_date": (
                        r.from_date.strftime("%Y-%m-%d %H:%M:%S")
                        if r.from_date
                        else None
                    ),
                    "To_date": (
                        r.to_date.strftime("%Y-%m-%d %H:%M:%S")
                        if r.to_date
                        else None
                    ),
                    "Last_sync": (
                        r.last_sync.replace(tzinfo=ZoneInfo("UTC"))
                        .astimezone(IST)
                        .isoformat()
                        if r.last_sync
                        else None
                    ),

                    "Missed_Calls": r.missed_calls or 0,
                    "Received_Calls": r.received_calls or 0,
                    "Outgoing_Calls": r.outgoing_calls or 0,
                    "Total_Duration": r.total_duration or 0,
                    "Admin_Team_Counts": r.admin_team_counts,
                }
            )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error during data formatting: {str(e)}",
        )

    return data