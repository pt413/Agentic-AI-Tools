from sqlalchemy import Column, String, DateTime
from datetime import datetime
from app.db.database import Base

class WhatsAppLidMapping(Base):
    __tablename__ = "whatsapp_lid_mappings"

    lid = Column(String(255), primary_key=True, index=True) # Bare LID or LID JID
    phone_number = Column(String(100), nullable=False, index=True) # Normalized PN
    admin_number = Column(String(100), index=True, nullable=True) # The admin who discovered it
    last_seen = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
