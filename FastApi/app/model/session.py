from sqlalchemy import Column, String, Boolean, Integer, DateTime, Text
from sqlalchemy.sql import func
from app.db.database import Base

class Session(Base):
    __tablename__ = "sessions"
    
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(100), unique=True, index=True, nullable=False)
    phone_number = Column(String(20), nullable=True)
    verified = Column(Boolean, default=False, nullable=False)
    status = Column(String(50), default="pending", nullable=False)
    stream_started = Column(Boolean, default=False, nullable=False)
    stream_pid = Column(Integer, nullable=True)
    stream_log_path = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
