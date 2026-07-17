from celery import Celery
from sqlalchemy.orm import Session
from app.db.database import get_db
from model.audio_file_model import AudioFile
from model.emails import Email
from FastApi.app.model.uni_activity import UnifiedData
from FastApi.app.model.user_data import UserData
import re


celery_app = Celery(
    "tasks",
    broker="redis://localhost:6379/0",  # adjust your Redis URL
)
# ---------------- Audio transfer ----------------
@celery_app.task
def transfer_call_transcripts_task():
    db: Session = next(get_db())  # get session
    # Pick only unsynced audio files
    audio_files = db.query(AudioFile).filter(AudioFile.sync_status == 0).all()
    if not audio_files:
        return {"success": True, "inserted_rows": 0}

    new_rows = []
    for row in audio_files:
        # Create destination row
        new_entry = UnifiedData(
            phone=row.customer_phone_number,
            role="customer",
            creation_time=row.uploaded_at,
            updation_time_at=row.uploaded_at,
        )
        new_rows.append(new_entry)
        # Mark source row as synced
        row.sync_status = 1

    # Insert and commit
    if new_rows:
        try:
            db.bulk_save_objects(new_rows)
            db.commit()
        except Exception as e:
            db.rollback()
            raise e

    return {"success": True, "inserted_rows": len(new_rows)}

@celery_app.task
def transfer_email_task():
    db: Session = next(get_db())  # Obtain the database session

    # Fetch emails that have not been synced yet (sync_status = 0)
    email_files = db.query(Email).filter(Email.sync_status == 0).all()

    if not email_files:
        return {"success": True, "inserted_rows": 0}

    new_rows = []
    
    for row in email_files:
        # Determine the role for sender and receiver based on the email domain
        sender_role = "admin" if re.search(r"@rentmystay\.com$", row.sender) else "customer"
        receiver_role = "admin" if re.search(r"@rentmystay\.com$", row.receiver) else "customer"
        sender_entry = UserData(
            name='',  
            phone='', 
            email=row.sender,  
            role=sender_role,
            creation_time=row.date,
            updation_time=row.date,  
        )
        new_rows.append(sender_entry)
        receiver_entry = UserData(
            name='',  
            phone='',  
            email=row.receiver,  
            role=receiver_role,
            creation_time=row.date,
            updation_time=row.date,  
        )
        new_rows.append(receiver_entry)

        row.sync_status = 1

    if new_rows:
        try:
            db.bulk_save_objects(new_rows)
            db.commit()
        except Exception as e:
            db.rollback()
            raise e
        
    return {"success": True, "inserted_rows": len(new_rows)}



# # ---------------- emails transfer ----------------
# @celery_app.task
# def transfer_email_task():
#     db: Session = next(get_db())

#     # Fetch emails that have not been synced yet
#     email_files = db.query(Email).filter(Email.sync_status == 0).all()

#     if not email_files:
#         return {"success": True, "inserted_rows": 0}

#     new_rows = []
#     for row in email_files:
#         # Determine the role based on the sender (if sender is a customer, role is 'customer', otherwise 'admin')
#         if re.search(r"@rentmystay\.com$", row.sender):  # The regex matches the domain at the end of the email
#             role = "admin"
#         else:
#             role = "customer"

#         # Insert data into the user_data table (instead of UnifiedData)
#         new_entry = UserData(
#             name='', 
#             phone='', 
#             email=row.sender,  
#             role=role,
#             creation_time=row.date,
#             updation_time=row.date,
#         )
#         new_rows.append(new_entry)
#         row.sync_status = 1

#     if new_rows:
#         try:
#             # Insert new rows into the user_data table
#             db.bulk_save_objects(new_rows)
#             db.commit()
#         except Exception as e:
#             db.rollback()
#             raise e

#     return {"success": True, "inserted_rows": len(new_rows)}

# def transfer_email_task():
#     db: Session = next(get_db())

#     email_files = db.query(Email).filter(Email.sync_status == 0).all()
#     if not email_files:
#         return {"success": True, "inserted_rows": 0}

#     new_rows = []
#     for row in email_files:
#         new_entry = UnifiedData(
#             cust_no=None,
#             admin_no=None,
#             sender=row.sender,
#             receiver=row.receiver,
#             department=None,
#             mode="emails",
#             direction=row.direction,
#             timestamp=row.date,
#             content=row.body or "",
#             subject=row.subject,
#             duration=None,
#         )
#         new_rows.append(new_entry)
#         # Mark source row as synced
#         row.sync_status = True

#     if new_rows:
#         try:
#             db.bulk_save_objects(new_rows)
#             db.commit()
#         except Exception as e:
#             db.rollback()
#             raise e

#     return {"success": True, "inserted_rows": len(new_rows)}