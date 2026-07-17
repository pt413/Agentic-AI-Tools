from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.db.database import get_db
from app.model.emails import Email
from app.model.email_credentials import GmailAccount
from sqlalchemy import union_all, select
from sqlalchemy import func, or_
router = APIRouter(prefix="/api/email_details", tags=["email_details"])

@router.get("/all")
def get_email_all_data(db: Session = Depends(get_db)):

    # 1. Fetch all registered emails
    all_registered_emails = db.query(GmailAccount.email).all()
    all_registered_emails = [email[0] for email in all_registered_emails]

    if not all_registered_emails:
        return {"message": "No registered emails found"}

    response = []
    for cred_email in all_registered_emails:
        # Count all emails sent OR received by this registered email
        total_emails = db.query(func.count(Email.id)).filter(
            or_(
                Email.sender == cred_email,
                Email.receiver == cred_email
            )
        ).scalar()

        # Get min/max dates for emails sent/received
        from_date = db.query(func.min(Email.date)).filter(
            or_(
                Email.sender == cred_email,
                Email.receiver == cred_email
            )
        ).scalar()

        to_date = db.query(func.max(Email.date)).filter(
            or_(
                Email.sender == cred_email,
                Email.receiver == cred_email
            )
        ).scalar()

        # Optional: get last sync status if you have it
        last_sync = db.query(func.max(Email.last_updated)).filter(
            or_(
                Email.sender == cred_email,
                Email.receiver == cred_email
            )
        ).scalar()

        response.append({
            "email": cred_email,
            "Total_Emails": total_emails or 0,
            "From_date": from_date,
            # "To_date": to_date,
            "Last_sync": last_sync
        })

    return response



@router.get("/count_by_date")
def get_email_count_by_date(email: str, date: str, db: Session = Depends(get_db)):
    from datetime import datetime

    try:
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
    except:
        return {"error": "Invalid date format. Use YYYY-MM-DD"}

    count = (
        db.query(Email)
        .filter(
            (Email.sender == email) | (Email.receiver == email),
            func.date(Email.date) == target_date
        )
        .count()
    )

    return {
        "email": email,
        "date": date,
        "count": count
    }
