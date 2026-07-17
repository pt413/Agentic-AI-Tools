from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime
from app.db.database import Base
from pgvector.sqlalchemy import Vector


class WhatsappChatSession(Base):
    __tablename__ = "whatsapp_chat_sessions"

    id = Column(Integer, primary_key=True, index=True)

    admin_phone = Column(String(100), nullable=False, index=True)
    customer_phone = Column(String(100), nullable=False, index=True)
    conversation_summary = Column(Text, nullable=True)
    context = Column(Text, nullable=True)
    outcome = Column(Text, nullable=True)
    language = Column(Text, nullable=True)
    intent = Column(Text, nullable=True)
    emotion = Column(Text, nullable=True)
    tone = Column(Text, nullable=True)
    actionable_signal = Column(Text, nullable=True)
    topic = Column(Text, nullable=True)
    start_time = Column(DateTime, index=True, nullable=True)   
    end_time = Column(DateTime, index=True, nullable=True)    
    summary_embedding = Column(Vector(384), nullable=True)  
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    sync_status=Column(Integer, nullable=False)
