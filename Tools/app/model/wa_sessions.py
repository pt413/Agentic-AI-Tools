from sqlalchemy import Column, String, Boolean, Integer, DateTime, Text
from sqlalchemy.sql import func
from app.db.database import Base

class WhatsAppSession(Base):
    __tablename__ = "wa_sessions"
    
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(100), unique=True, index=True, nullable=False)
    phone_number = Column(String(20), nullable=True)
    verified = Column(Boolean, default=False, nullable=False)
    status = Column(String(50), default="pending", nullable=False)
    is_active = Column(Boolean, default=False, nullable=False)
    
    last_connected_at = Column(DateTime(timezone=True), nullable=True)
    last_disconnected_at = Column(DateTime(timezone=True), nullable=True)
    
    stream_started = Column(Boolean, default=False, nullable=False)
    stream_pid = Column(Integer, nullable=True)
    stream_log_path = Column(Text, nullable=True)

    stream_run_id = Column(String(150), nullable=True, index=True)
    socket_state = Column(String(50), nullable=True)  # open / close / connecting
    socket_ready = Column(Boolean, default=False, nullable=False)

    last_heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    last_socket_open_at = Column(DateTime(timezone=True), nullable=True)
    last_socket_close_at = Column(DateTime(timezone=True), nullable=True)

    last_message_upsert_at = Column(DateTime(timezone=True), nullable=True)
    message_upsert_count = Column(Integer, default=0, nullable=False)

    last_disconnect_code = Column(Integer, nullable=True)
    last_disconnect_reason = Column(Text, nullable=True)
    last_health_reason = Column(String(100), nullable=True)

    reconnect_count = Column(Integer, default=0, nullable=False)    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
