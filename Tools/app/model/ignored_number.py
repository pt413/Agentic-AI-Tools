from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func
from app.db.database import Base

class IgnoredNumber(Base):
    __tablename__ = "ignored_numbers"
    
    id = Column(Integer, primary_key=True, index=True)
    number = Column(String(20), unique=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
