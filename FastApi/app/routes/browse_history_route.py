# app/router/browsing_history.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, case
from sqlalchemy.dialects.postgresql import JSONB
from app.db.database import get_db
from app.model.lead_history import LeadHistory as lh

router = APIRouter(prefix="/api/browsing_history", tags=["Browsing_History"])

@router.get("/")
def get_browsing_history_summary(db: Session = Depends(get_db)):
    try:
        # Subquery to count pages by source (replace NULL sources)
        source_count_subq = (
            db.query(
                lh.lead_id.label("lead_id"),
                func.coalesce(lh.source, "Unknown").label("source"),
                func.count(lh.id).label("source_count")
            )
            .group_by(lh.lead_id, func.coalesce(lh.source, "Unknown"))
            .subquery()
        )

        # Convert per-lead source counts to JSON
        source_json = (
            db.query(
                source_count_subq.c.lead_id,
                func.jsonb_object_agg(
                    source_count_subq.c.source,
                    source_count_subq.c.source_count
                ).label("source_counts")
            )
            .group_by(source_count_subq.c.lead_id)
            .subquery()
        )

        # Main browsing summary
        summary_subq = (
            db.query(
                lh.lead_id.label("lead_id"),
                func.count(lh.id).label("total_visits"),
                func.min(lh.timestamp).label("first_visit"),
                func.max(lh.timestamp).label("last_visit"),
                func.count(func.distinct(lh.current_page)).label("unique_pages"),
                func.max(lh.timestamp).label("last_sync"),
                func.count(func.distinct(lh.ip_address)).label("unique_ips"),
                func.count(func.distinct(lh.area)).label("unique_areas"),
            )
            .group_by(lh.lead_id)
            .subquery()
        )

        # Combine both summaries
        results = (
            db.query(
                summary_subq.c.lead_id,
                summary_subq.c.total_visits,
                summary_subq.c.first_visit,
                summary_subq.c.last_visit,
                summary_subq.c.unique_pages,
                summary_subq.c.last_sync,
                summary_subq.c.unique_ips,
                summary_subq.c.unique_areas,
                func.coalesce(source_json.c.source_counts, func.cast('{}', JSONB)).label("source_counts")
            )
            .outerjoin(source_json, summary_subq.c.lead_id == source_json.c.lead_id)
            .all()
        )

        # Convert to serializable dicts
        data = []
        for r in results:
            data.append({
                "Lead_ID": r.lead_id,
                "Total_Visits": r.total_visits,
                "First_Visit": r.first_visit.strftime("%Y-%m-%d %H:%M:%S") if r.first_visit else None,
                "Last_Visit": r.last_visit.strftime("%Y-%m-%d %H:%M:%S") if r.last_visit else None,
                "Unique_Pages": r.unique_pages,
                "Unique_IPs": r.unique_ips,
                "Unique_Areas": r.unique_areas,
                "Last_Sync": r.last_sync.strftime("%Y-%m-%d %H:%M:%S") if r.last_sync else None,
                "Source_Counts": r.source_counts,
            })

        return data

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching browsing history: {str(e)}")
