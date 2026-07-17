from datetime import datetime
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from app.db.database import SessionLocal
from app.model.user_data import UserData
from app.model.uni_activity import UnifiedData

BATCH_SIZE = 200
COMMIT_EVERY = 50
ROLE_CUSTOMER = "customer"
COMMIT_EVERY = 50

from datetime import datetime
from sqlalchemy.exc import SQLAlchemyError
from app.db.database import SessionLocal
from app.model.audio_file_model import AudioFile

def populate_uni_activity(limit=None):
    db = SessionLocal()
    
    processed = 0
    inserted_users = 0
    inserted_activity = 0

    try:
        query = db.query(AudioFile).filter(AudioFile.sync_status == 0).order_by(AudioFile.call_datetime.asc()).limit(10)
        if limit:
            query = query.limit(limit)
        rows = query.all()

        for row in rows:
            processed += 1
            now = datetime.utcnow()

            try:
                # Fetch or insert customer
                cust_user = None
                if row.customer_phone_number:
                    cust_user = db.query(UserData).filter(UserData.phone == row.customer_phone_number).first()
                    if not cust_user:
                        cust_user = UserData(
                            name=None,
                            phone=row.customer_phone_number,
                            email=None,
                            role="customer",
                            creation_time=now,
                            updation_time=now,
                            org_id=""
                        )
                        db.add(cust_user)
                        db.flush() 
                        # db.commit() # Ensure u_id is generated
                        inserted_users += 1

                # Fetch or insert admin
                admin_user = db.query(UserData).filter(UserData.phone == row.emp_phone_number).first()
                if not admin_user:
                    admin_user = UserData(
                        name=None,
                        phone=row.emp_phone_number,
                        email=None,
                        role="admin",
                        creation_time=now,
                        updation_time=now,
                        org_id=""
                    )
                    db.add(admin_user)
                    db.flush()  
                    inserted_users += 1

                # Determining sender and receiver based on direction
                if row.call_type and row.call_type.lower() == "incoming":
                    sender_name = row.customer_phone_number
                    receiver_name = row.emp_phone_number
                    sender_id = cust_user.u_id if cust_user else None
                    receiver_id = admin_user.u_id if admin_user else None
                else: 
                    sender_name = row.emp_phone_number
                    receiver_name = row.customer_phone_number
                    sender_id = admin_user.u_id if admin_user else None
                    receiver_id = cust_user.u_id if cust_user else None

                print("row.id", row.id, "admin_phone", row.emp_phone_number, "admin_u_id", admin_user.u_id)


                uni_entry = UnifiedData(
                    sen_id=cust_user.u_id if cust_user else None,
                    rec_id=admin_user.u_id if admin_user else None,
                    sender=sender_name,
                    receiver=receiver_name,
                    channel="call",
                    content=row.transcript_text or "",
                    timestamp=row.call_datetime,
                    direction=row.call_type,
                    meta_data=None,
                    embed_id=None
                )
                db.add(uni_entry)
                inserted_activity += 1

                row.sync_status = 1
                db.add(row)

                db.commit()

            except SQLAlchemyError as e:
                db.rollback()
                print(f"Error processing row id={row.id}: {e}")

    finally:
        db.close()

    print(f"Processed rows: {processed}")
    print(f"Inserted new users: {inserted_users}")
    print(f"Inserted uni_activity records: {inserted_activity}")

if __name__ == "__main__":
    print(populate_uni_activity(limit=10))
