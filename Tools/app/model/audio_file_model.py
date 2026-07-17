from sqlalchemy import Column, Integer, String, DateTime, Text, Index, text, JSON, Boolean, BigInteger, TIMESTAMP
from datetime import datetime
from sqlalchemy.sql import func
from app.db.database import Base
from pgvector.sqlalchemy import Vector

class AudioFile(Base):
    __tablename__ = "call_recordings_transcript"

    id = Column(Integer, primary_key=True, index=True)
    emp_phone_number = Column(String, nullable=True, index=True)
    call_id = Column(String(100), nullable=True, index=True)
    emp_name = Column(String, nullable=True)
    customer_phone_number = Column(String, nullable=True)
    call_datetime = Column(DateTime(timezone=True), nullable=False)
    call_duration = Column(Integer, nullable=True, default=0)
    call_type = Column(String, nullable=True)
    department = Column(String, nullable=True)
    audio_url = Column(String, nullable=True, index=True)
    transcript_text = Column(Text, nullable=True)
    filename = Column(String, nullable=True)
    uploaded_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=True)
    status = Column(Integer, default=0, server_default='0', nullable=False)
    transcript_text_eleven_labs=Column(String, nullable=True)
    raw_eleven_labs_transcript=Column(JSON, nullable=True)
    sync_status=Column(Integer, nullable=False, default=0)
    distinct_cus_ph = Column(String, nullable=True)
    raw_transcripts = Column(Text, nullable=True,default=None)
    translated_text = Column(Text, nullable=True)

    __table_args__ = (
        Index(
            'idx_emp_phone_callid_unique',
            'emp_phone_number',
            'call_id',
            unique=True,
            postgresql_where=text("call_id IS NOT NULL")
        ),
    )
class Location(Base):
    __tablename__ = "location"
    
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    sales_phone_number = Column(String, nullable=True, index=True)
    location = Column(Text, nullable=True)
    time = Column(DateTime(timezone=True), nullable=True)

# class SalesCallLog(Base):
#     __tablename__ = "sales_call_log"
    
#     id = Column(Integer, primary_key=True, index=True)
#     username = Column(String)
#     phNum = Column(String)
#     salesPhoneNumber = Column(String, index=True)
#     callType = Column(String)
#     callDuration = Column(Integer)
#     lead_id = Column(String)
#     timestamp = Column(BigInteger)
#     callDate = Column(TIMESTAMP)
#     added_on = Column(TIMESTAMP)
#     admin_Team = Column(String)
#     status = Column(Integer, default=0, server_default='0', nullable=False)  # ✅ NEW FIELD
