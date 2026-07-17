#This file handles the transfer of raw data to unified table
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Body
from fastapi.responses import JSONResponse
import requests, time, os, aiofiles
from elevenlabs.client import ElevenLabs
from typing import  List
import requests
import aiohttp
from io import BytesIO
from dotenv import load_dotenv
from pydantic import BaseModel
from urllib.parse import urlparse
from typing import List, Dict, Any
import boto3
from app.model.audio_file_model import AudioFile
from app.model.uni_activity import UnifiedData
from app.model.user_data import UserData
from app.model.emails import Email
# from app.model.lead_details import LeadActivity
from db.database import Session, get_db
from sqlalchemy.orm import Session
import google.generativeai as genai
import json
import re
from datetime import datetime
# from sqlalchemy import text
# from sqlalchemy.exc import IntegrityError
from app.db.database import SessionLocal
from app.model.audio_file_model import AudioFile

class AudioIDRequest(BaseModel):
    id: int

class UnifyTableSchema(BaseModel):
    cus_no: str

router = APIRouter(prefix="/api/transfer", tags=["Transfer"])

@router.post("/call_recordings_transcript")
async def transfer_call_transcripts_data(db: Session = Depends(get_db)):

    audio_files = db.query(
        AudioFile.customer_phone_number,
        AudioFile.department,
        AudioFile.emp_phone_number,
        AudioFile.call_type,
        AudioFile.uploaded_at,
        AudioFile.transcript_text,
        AudioFile.call_duration
    ).limit(21).all()

    if not audio_files:
        raise HTTPException(status_code=404, detail="Audio file not found")

    new_rows = []

    for row in audio_files:
        # Duplicate check
        try:
            exists = db.query(UnifiedData).filter(
                UnifiedData.cust_no == row.customer_phone_number,
                UnifiedData.timestamp == row.uploaded_at,
                UnifiedData.mode == "call_transcript"
            ).first()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Duplicate check DB error: {str(e)}")

        if exists:
            continue  # skip duplicates

        # Create destination row
        new_entry = UnifiedData(
            cust_no=row.customer_phone_number,
            admin_no=row.emp_phone_number,
            sender="",
            receiver="",
            department=row.department,
            mode="call_transcript",
            direction=row.call_type,
            timestamp=row.uploaded_at,
            content=row.transcript_text or "",
            subject=None,
            duration=row.call_duration,
        )

        new_rows.append(new_entry)

    # Insert all rows
    if new_rows:
        try:
            db.bulk_save_objects(new_rows)   # USE BULK INSERT
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Insert error: {str(e)}")

    return {
        "success": True,
        "inserted_rows": len(new_rows)
    }



# @router.post("/email")
# async def transfer_email_data(db: Session = Depends(get_db)):
#     email_files = db.query(
#         Email.sender,
#         Email.receiver,
#         Email.direction,
#         Email.date,
#         Email.body,
#         Email.subject,  
#         ).limit(21).all()

#     if not email_files:
#         raise HTTPException(status_code=404, detail="Audio file not found")

#     new_rows = []

#     for row in email_files:
#         # Duplicate check
#         try:
#             exists = db.query(UnifiedData).filter(
#                 UnifiedData.sender == row.sender,
#                 UnifiedData.receiver == row.receiver,
#                 UnifiedData.timestamp == row.date,
#                 UnifiedData.subject == row.subject
#             ).first()
#         except Exception as e:
#             raise HTTPException(status_code=500, detail=f"Duplicate check DB error: {str(e)}")

#         if exists:
#             continue  # skip duplicates

#         # Create destination row
#         new_entry = UnifiedData(
#             cust_no=None,
#             admin_no=None,
#             sender=row.sender,
#             receiver=row.receiver,
#             department=None,
#             mode="email",
#             direction=row.direction,
#             timestamp=row.date,
#             content=row.body or "",
#             subject=row.subject,
#             duration=None,
#         )

#         new_rows.append(new_entry)

#     # Insert all rows
#     if new_rows:
#         try:
#             db.bulk_save_objects(new_rows)   # USE BULK INSERT
#             db.commit()
#         except Exception as e:
#             db.rollback()
#             raise HTTPException(status_code=500, detail=f"Insert error: {str(e)}")

#     return {
#         "success": True,
#         "inserted_rows": len(new_rows)
#     }
@router.post("/email")
async def transfer_email_task(db: Session = Depends(get_db)):
    email_files = db.query(Email).filter(Email.sync_status == 0).limit(10).all()

    if not email_files:
        return {"success": True, "inserted_rows": 0}

    new_rows = []

    for row in email_files:
        # Determine roles based on email domain
        sender_role = "admin" if re.search(r"@rentmystay\.com$", row.sender) else "customer"
        receiver_role = "admin" if re.search(r"@rentmystay\.com$", row.receiver) else "customer"

        # Sender perspective
        sender_entry = UserData(
            name=row.subject or "",
            phone="",  # You can assign a phone if available
            email=row.sender,
            role=sender_role,
            creation_time=row.date,
            updation_time=row.date,
            org_id="",
        )
        new_rows.append(sender_entry)

        # Receiver perspective
        receiver_entry = UserData(
            name=row.subject or "",
            phone="",
            email=row.receiver,
            role=receiver_role,
            creation_time=row.date,
            updation_time=row.date,
            org_id="",
        )
        new_rows.append(receiver_entry)

        # Mark email as synced
        row.sync_status = 1
        # db.add(row)

    # Insert into UserData
    try:
        db.bulk_save_objects(new_rows)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Insertion failed: {str(e)}")

    return {"success": True, "inserted_rows": len(new_rows)}
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
    # Fetch emails that have not been synced yet
    # email_files = db.query(Email).filter(Email.sync_status == 0).limit(10).all()

    # if not email_files:
    #     return {"success": True, "inserted_rows": 0}

    # new_rows = []

    # for row in email_files:
    #     try:
    #         # Determine role based on email domain
    #         sender_role = "admin" if re.search(r"@rentmystay\.com$", row.sender or "") else "customer"
    #         receiver_role = "admin" if re.search(r"@rentmystay\.com$", row.receiver or "") else "customer"

    #         sender_entry = UserData(
    #             name='',
    #             phone='',
    #             email=row.sender,
    #             role=sender_role,
    #             creation_time=row.date or datetime.utcnow(),
    #             updation_time=row.date or datetime.utcnow(),
    #             org_id='',
    #         )
    #         new_rows.append(sender_entry)

    #         receiver_entry = UserData(
    #             name='',
    #             phone='',
    #             email=row.receiver,
    #             role=receiver_role,
    #             creation_time=row.date or datetime.utcnow(),
    #             updation_time=row.date or datetime.utcnow(),
    #             org_id='',
    #         )
    #         new_rows.append(receiver_entry)

    #         # Mark email as synced
    #         row.sync_status = 1
    #         db.add(row)

    #     except Exception as e:
    #         print("Row processing error:", e)
    #         continue  # skip problematic row

    # if new_rows:
    #     try:
    #         db.add_all(new_rows)  # safer than bulk_save_objects
    #         db.commit()
    #     except Exception as e:
    #         db.rollback()
    #         print("Insert error:", e)
    #         raise HTTPException(status_code=500, detail=f"Insert error: {str(e)}")

    # return {"success": True, "inserted_rows": len(new_rows)}

# @router.post("/email")
# async def transfer_email_task(db: Session = Depends(get_db)):
#     # db: Session = next(get_db())  # Obtain the database session

#     # Fetch emails that have not been synced yet (sync_status = 0)
#     email_files = db.query(Email).filter(Email.sync_status == 0).limit(10).all()

#     if not email_files:
#         return {"success": True, "inserted_rows": 0}

#     new_rows = []
    
#     for row in email_files:
#         # Determine the role for sender and receiver based on the email domain
#         sender_role = "admin" if re.search(r"@rentmystay\.com$", row.sender) else "customer"
#         receiver_role = "admin" if re.search(r"@rentmystay\.com$", row.receiver) else "customer"
#         sender_entry = UserData(
#             name='',  
#             phone='', 
#             email=row.sender,  
#             role=sender_role,
#             creation_time=row.date,
#             updation_time=row.date,
#             org_id='',  
#         )
#         new_rows.append(sender_entry)
#         receiver_entry = UserData(
#             name='',  
#             phone='',  
#             email=row.receiver,  
#             role=receiver_role,
#             creation_time=row.date,
#             updation_time=row.date,  
#             org_id='',  
#         )
#         new_rows.append(receiver_entry)

#         # row.sync_status = 1

#     if new_rows:
#         try:
#             db.bulk_save_objects(new_rows)
#             db.commit()
#         except Exception as e:
#             db.rollback()
#             raise e
        
#     return {"success": True, "inserted_rows": len(new_rows)}


# @router.post("/whatsapp")


# BATCH_SIZE = 200
# COMMIT_EVERY = 50
# ROLE_CUSTOMER = "customer"

# def normalize_phone(p):
#     if not p:
#         return None
#     return p.strip()

# def main(limit=None):
#     db = SessionLocal()
#     processed = 0
#     inserted = 0
#     skipped = 0
#     buffered = 0

#     try:
#         q = db.query(AudioFile).filter(AudioFile.sync_status == 0).order_by(AudioFile.call_datetime.asc())
#         if limit:
#             q = q.limit(limit)
#         rows = q.all()

#         for r in rows:
#             processed += 1
#             created_any = False

#             cust_phone = normalize_phone(r.customer_phone_number)
#             now = datetime.utcnow()

#             if cust_phone:
                
#                 res = db.execute(
#                     text("SELECT u_id FROM user_data WHERE phone = :phone LIMIT 1"),
#                     {"phone": cust_phone}
#                 ).first()

#                 if not res:
#                     try:
#                         db.execute(
#                             text(
#                                 "INSERT INTO user_data (name, phone, email, role, creation_time, updation_time) "
#                                 "VALUES (:name, :phone, :email, :role, :creation_time, :updation_time)"
#                             ),
#                             {
#                                 "name": None,
#                                 "phone": cust_phone,
#                                 "email": None,
#                                 "role": ROLE_CUSTOMER,
#                                 "creation_time": now,
#                                 "updation_time": now,
#                             },
#                         )
#                         inserted += 1
#                         created_any = True
#                     except IntegrityError:
                        
#                         db.rollback()
#                 else:
                    
#                     skipped += 1

#             else:
                
#                 skipped += 1

            
#             r.sync_status = 1
#             db.add(r)

#             buffered += 1

#             if buffered >= COMMIT_EVERY:
#                 try:
#                     db.commit()
#                 except Exception:
#                     db.rollback()
#                 finally:
#                     buffered = 0

        
#         if buffered > 0:
#             try:
#                 db.commit()
#             except Exception:
#                 db.rollback()

#         print(f"processed={processed}, inserted={inserted}, skipped={skipped}")

#     finally:
#         db.close()


# @router.post("/crm")
# async def transfer_crm_data(db: Session = Depends(get_db)):

#     lead_rows = db.query(
#         LeadActivity.lead_id,
#         LeadActivity.user_id,
#         LeadActivity.timestamp,
#         LeadActivity.source,
#         LeadActivity.referal_page,
#         LeadActivity.current_page,
#         LeadActivity.ip_address,
#         LeadActivity.session_id,
#         LeadActivity.user_agent,
#         LeadActivity.area,
#         LeadActivity.name,
#         LeadActivity.building_name,
#         LeadActivity.furnishing_type,
#         LeadActivity.unit_type
#     ).limit(20).all()

#     if not lead_rows:
#         raise HTTPException(status_code=404, detail="No LeadActivity rows available")

#     new_rows = []

#     for row in lead_rows:

#         # --- Duplicate Check ---
#         try:
#             exists = db.query(UnifiedData).filter(
#                 UnifiedData.sender == row.user_id,
#                 UnifiedData.timestamp == row.timestamp,
#                 UnifiedData.mode == "lead_history"
#             ).first()
#         except Exception as e:
#             raise HTTPException(status_code=500, detail=f"Duplicate check error: {str(e)}")

#         if exists:
#             continue

#         # Prepare content JSON 
#         content_json = json.dumps({
#             "referal_page": row.referal_page,
#             "current_page": row.current_page,
#             "ip_address": row.ip_address,
#             "session_id": row.session_id,
#             "user_agent": row.user_agent,
#             "area": row.area,
#             "name": row.name,
#             "building_name": row.building_name,
#             "furnishing_type": row.furnishing_type,
#             "unit_type": row.unit_type,
#         })

#         # --- Create new unified row ---
#         new_entry = UnifiedData(
#             cust_no=row.lead_id,
#             admin_no=None,
#             sender=row.user_id,
#             receiver=None,
#             department=row.source or "",
#             mode="lead_history",
#             direction=None,
#             timestamp=row.timestamp,
#             content=content_json,
#             subject=None,
#             duration=None,
#         )

#         new_rows.append(new_entry)

#     # --- Bulk Insert ---
#     if new_rows:
#         try:
#             db.bulk_save_objects(new_rows)
#             db.commit()
#         except Exception as e:
#             db.rollback()
#             raise HTTPException(status_code=500, detail=f"Insertion error: {str(e)}")

#     return {
#         "success": True,
#         "inserted_rows": len(new_rows)}
