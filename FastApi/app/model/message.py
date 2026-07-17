from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, Index
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime
from app.db.database import Base
from pgvector.sqlalchemy import Vector



class Message(Base):
    __tablename__ = "messages"
    
    # __table_args__ = (
    #     Index('idx_admin_timestamp', 'admin_number', 'timestamp'),
    #     Index('idx_admin_cx_timestamp', 'admin_number', 'cx_number', 'timestamp'),
    #     Index('idx_admin_participant', 'admin_number', 'participant'),
    #     Index('idx_admin_peer_pn', 'admin_number', 'peer_pn'),
    # )

    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(String(255), unique=True, nullable=False, index=True)
    admin_number = Column(String(100), nullable=False, index=True)
    cx_number = Column(String(100), nullable=False, index=True)
    content = Column(String, nullable=True)
    clean_content = Column(String, nullable=True)

    # context = Column(Text, nullable=True)
    # outcome = Column(Text, nullable=True)
    # language = Column(Text, nullable=True)  
    # intent = Column(Text, nullable=True)
    # emotion = Column(Text, nullable=True)
    # tone = Column(Text, nullable=True)
    # actionable_signal = Column(Text, nullable=True)
    # topic = Column(Text, nullable=True)


    direction = Column(String(20), nullable=False)  # incoming / outgoing
    message_type = Column(String(50), default="text")
    # The timestamp represents when the message was actually sent/received
    # Do NOT default to the current time here so the value from the source
    # (Baileys helper) is preserved when provided in the API payload.
    timestamp = Column(DateTime, index=True, nullable=True)
    # last_sync remains the time we synced/inserted into our DB
    last_sync = Column(DateTime, default=datetime.utcnow, index=True)
    device = Column(String(50), nullable=True)
    isread = Column(Boolean, default=False)
    issent = Column(Boolean, default=False)
    remote_jid = Column(String(255), nullable=True, index=True)
    raw = Column(JSONB, nullable=True)

    r2_media_url = Column(Text, nullable=True)

    participant = Column(String(255), nullable=True, index=True) # JID of the sender (esp in groups)
    peer_pn = Column(String(100), nullable=True, index=True) # Resolved PN for LID participant
    
    # OCR FIELDS
    
    ocr_status = Column(String(20), nullable=True, default="no_media", index=True)
    extracted_text = Column(Text, nullable=True)
    image_type = Column(String(100), nullable=True, index=True)
    
    # clean_content_embedding= Column(Vector(384), nullable=True)
    sync_status=Column(Integer, nullable=False, default=0)