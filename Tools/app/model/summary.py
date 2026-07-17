from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()

class Summary(Base):
    __tablename__ = "summary"

    id = Column(Integer, primary_key=True, index=True)
    admin_number = Column(String, index=True, nullable=True)
    cx_number = Column(String, index=True, nullable=True)
    cx_email = Column(String, nullable=True)
    lead_id = Column(String, nullable=True)
    lead_status = Column(String, nullable=True)

    wa_summary = Column(Text, nullable=True)
    email_summary = Column(Text, nullable=True)
    call_summary = Column(Text, nullable=True)

    created_on = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String, nullable=True)
    last_updated_on = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

