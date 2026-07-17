from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.database import SessionLocal, get_db
from app.services.email_service import EmailService

router = APIRouter(prefix="/api/email", tags=["Gmail"])

@router.get("/fetch/{email_address}")
async def fetch_email_route(email_address: str, db: Session = Depends(get_db)):
    try:
        service = EmailService(db)
        return service.fetch_emails_for_address(email_address)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/emails/summary/{cx_email}")
async def get_email_summary(cx_email: str, db: Session = Depends(get_db)):
    try:
        service = EmailService(db)
        return await service.get_summary(cx_email)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@router.post("/cron/fetch_all")
def cron_fetch_all_emails(db: Session = Depends(get_db)):
    svc = EmailService(db)
    results = svc.fetch_all_accounts()
    all_errors = all(r.get("status") == "error" for r in results)
    if all_errors:
        raise HTTPException(status_code=500, detail={"results": results})
    return {"status": "ok", "results": results}




'''from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pgvector.sqlalchemy import Vector
from app.db.database import SessionLocal
from app.model.summary import Summary
from app.model.emails import Email  # your SQLAlchemy model
from app.generic.fetch_email import gmail_authenticate, fetch_emails
from app.services.email_summary import fetch_emails_for_customer, summarize_customer_emails
from datetime import datetime, timedelta, timezone as dt_timezone
import pytz
from app.generic.llm_ans import gemini

router = APIRouter(prefix="/api/email", tags=["Gmail"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/fetch")
async def fetch_email_route( db: Session = Depends(get_db)):
    # print(f"emails{mail}")
    utc_now = datetime.now(dt_timezone.utc)
    ist = pytz.timezone('Asia/Kolkata')
    now_ist = utc_now.astimezone(ist)
    thirty_days_ago_ist = now_ist - timedelta(days=1)

    after_timestamp = int(thirty_days_ago_ist.timestamp())
    before_timestamp = int(now_ist.timestamp())

    # query = f"(from:{mail} OR to:contact@rentmystay.com) (in:inbox OR in:sent)"
    # query = f"subject:(Re: Action required for Refund B52390 - RentMyStay) bbhavaniclassic1996@gmail.com(in:inbox OR in:sent)"
    # query = f"subject:(Re: Confirm move out for B52067 - RentMyStay)(in:inbox OR in:sent)"
    query = f"after:{after_timestamp} before:{before_timestamp} (in:inbox OR in:sent)"
    # query=f"((from:{mail} AND to:contact@rentmystay.com OR from:help@rentmystay.com)) (in:inbox OR in:sent)"
    # query = f"((from:{mail} to:contact@rentmystay.com) OR (from:{mail} to:help@rentmystay.com) OR (to:{mail} from:contact@rentmystay.com) OR (to:{mail} from:help@rentmystay.com)) (in:inbox OR in:sent)"
    service = gmail_authenticate()
    emails = fetch_emails(service, query=query)
    print("fetching emails ")
    db_emails = []
    for e in emails:
        # Convert date string with timezone offset
        try:
            date_obj = datetime.strptime(e['date'], '%Y-%m-%d %H:%M:%S %Z')
        except:
            try:
                date_obj = datetime.fromisoformat(e['date'])
            except:
                date_obj = now_ist  # fallback

        db_emails.append(Email(
            subject=e['subject'],
            direction=e['direction'],
            sender=e['sender'],
            receiver=e['receiver'],
            date=date_obj,
            body=e['body'],
            msgid=e['msgid'],
            thread_id=e['thread_id'],
            # embedding=e['embedding'],
            # subject_emb=e['subject_emb']
        ))

 
        # snippet=e['snippet'],
            #   summary=e['summary']
    # data=f"data : {db_emails}"
    # print(f"data {db_emails}")

    # return {"okay "}
    # question=input("Enter the your query")
    # emails_text = ""
    # for e in db_emails:
    #     emails_text += f"Subject: {e.subject}\nFrom: {e.sender}\nTo: {e.receiver}\nDate: {e.date}\nBody:\n{e.body}\n{'-'*50}\n"
    
    # question=input("Enter the your query")
    # response = await gemini(emails_text, question)

    # print(response)
    #         # 
    db.bulk_save_objects(db_emails)
    db.commit()
    print(f"Total emails found : {len(db_emails)}")
    return {"emails": db_emails}


@router.get("/emails/summary/{cx_email}")
async def get_email_summary(cx_email: str, db: Session = Depends(get_db)):
    try:
        all_emails_text = await fetch_emails_for_customer(cx_email, db)
        print(f"Fetched emails: {len(all_emails_text)} chars")  # debug

        final_summary = await summarize_customer_emails(all_emails_text)
        print(f"Generated summary: {len(final_summary)} chars")  # debug

        summary_obj = db.query(Summary).filter(Summary.cx_email == cx_email).first()
        if summary_obj:
            summary_obj.email_summary = final_summary
        else:
            summary_obj = Summary(
                cx_email=cx_email,
                email_summary=final_summary,
                created_on=datetime.now()
            )
            db.add(summary_obj)

        db.commit()
        db.refresh(summary_obj)

        return {
            "cx_email": cx_email,
            "email_summary": final_summary
        }

    except Exception as e:
        print(f"ERROR in get_email_summary: {e}")
        raise'''
