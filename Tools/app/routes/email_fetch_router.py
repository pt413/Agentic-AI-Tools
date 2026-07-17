from fastapi import APIRouter, Query, HTTPException
from typing import Optional
from datetime import datetime
from sqlalchemy import text
from app.db.database import SessionLocal

router = APIRouter(prefix="/emails", tags=["Email Fetch"])


@router.get("/range")
def fetch_emails_by_range(
    start_date: str = Query(..., description="YYYY-MM-DD"),
    end_date: str = Query(..., description="YYYY-MM-DD"),
    risk_label: Optional[str] = None,
    only_risky: bool = False,
    limit: int = 100,
    offset: int = 0,
):
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    db = SessionLocal()

    try:
        query = """
        SELECT
            e.id AS email_id,
            e.subject,
            e.sender,
            e.receiver,
            e.date,
            e.body,
            er.risk_label,
            er.risk_score,
            er.similarity_score,
            er.validated_by_llm
        FROM email_risk_analysis er
        JOIN rag_embeddings r
            ON er.rag_embedding_id = r.id
        JOIN emails e
            ON e.id = CAST(SUBSTRING(r.source_id FROM 8) AS INTEGER)
        WHERE e.date BETWEEN :start_dt AND :end_dt
        """

        params = {
            "start_dt": start_dt,
            "end_dt": end_dt,
            "limit": limit,
            "offset": offset,
        }

        if only_risky:
            query += " AND er.risk_label != 'skipped' "

        if risk_label:
            query += " AND er.risk_label = :risk_label "
            params["risk_label"] = risk_label

        query += " ORDER BY e.date DESC LIMIT :limit OFFSET :offset "

        result = db.execute(text(query), params)
        rows = result.fetchall()

        return {
            "count": len(rows),
            "data": [dict(row._mapping) for row in rows],
        }

    finally:
        db.close()