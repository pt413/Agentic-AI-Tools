from sqlalchemy import Column, String, DateTime, Text
from sqlalchemy.sql import func
from app.db.database import Base

class Msg(Base):
    __tablename__ = "msg"

    message_id = Column(String, primary_key=True, index=True)
    direction = Column(String, index=True)  # inbound/outbound
    sender = Column(String, index=True)
    recipient = Column(String, index=True)
    message_type = Column(String)
    text = Column(Text)
    status = Column(String)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
