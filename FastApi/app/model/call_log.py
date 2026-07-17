# app/model/call_log.py
from sqlalchemy import Column, Integer, BigInteger, String, DateTime, Index
from datetime import datetime
from app.db.database import Base

class CallLog(Base):
    __tablename__ = "sales_call_log"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), nullable=True, index=True)          # Admin name (merged)
    phNum = Column(String(20), nullable=True, index=True)               # Customer phone number
    salesPhoneNumber = Column(String(20), nullable=True, index=True)   # Admin's phone number
    callType = Column(String(50), nullable=True)
    callDuration = Column(Integer, nullable=True, default=0)
    lead_id = Column(String(50), nullable=True, index=True)
    timestamp = Column(BigInteger, nullable=True, index=True)
    callDate = Column(DateTime, nullable=True, index=True)
    added_on = Column(DateTime, default=datetime.utcnow, index=True)
    admin_Team = Column(String(100), nullable=True, default="Unknown") # Admin team (merged)
    sync_status=Column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index('idx_username_phnum', 'username', 'phNum'),
        Index('idx_sales_phone', 'salesPhoneNumber'),
        Index('idx_lead_calltype', 'lead_id', 'callType'),
        Index('idx_admin_team', 'admin_Team'),
    )

    def __repr__(self):
        return f"<CallLog username={self.username}, phNum={self.phNum}, salesPhoneNumber={self.salesPhoneNumber}>"
