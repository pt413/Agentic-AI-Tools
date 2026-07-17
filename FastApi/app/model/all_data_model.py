from sqlalchemy import Column, Integer, Text, JSON, TIMESTAMP
from sqlalchemy.sql import func
from pgvector.sqlalchemy import Vector
from app.db.database import Base   # correct path

from pgvector.sqlalchemy import Vector
class CustomerRecord(Base):
    __tablename__ = "customer_record"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(Text, index=True)


    booking_json = Column(JSON)
    emails_json = Column(JSON)
    whatsapp_json = Column(JSON)
    call_logs_json = Column(JSON)

    booking_status = Column(Text)
    primary_contact = Column(Text)
    primary_email = Column(Text)
    prop_id = Column(Text)
    prop_name = Column(Text)
    travel_from_date = Column(TIMESTAMP)
    travel_to_date = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)

 

    booking_vector = Column(Vector(384))
    email_vector = Column(Vector(384))
    whatsapp_vector = Column(Vector(384))
    calls_vector = Column(Vector(384))


    created_at = Column(TIMESTAMP, server_default=func.now())
