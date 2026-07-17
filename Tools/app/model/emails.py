from pgvector.sqlalchemy import Vector  
from sqlalchemy import Column, Integer, String, DateTime, JSON,Boolean
from app.db.database import Base
from datetime import datetime as time
class Email(Base):
    __tablename__ = "emails"

    id = Column(Integer, primary_key=True, index=True)
    subject = Column(String)
    direction = Column(String)
    sender = Column(String)
    receiver = Column(String)
    date = Column(DateTime)
    body = Column(String)
    snippet = Column(String)
    msgid = Column(String, unique=True)
    thread_id = Column(String)
    summary = Column(String)
    last_updated = Column(DateTime, default=time.utcnow)
    embedding = Column(Vector(384), nullable=True)  # for all-MiniLM-L6-v2
    sync_status=Column(Integer, nullable=False, default=0)


