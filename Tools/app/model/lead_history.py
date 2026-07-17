# app/model/lead_history.py
from sqlalchemy import Column, Integer, String, DateTime
from datetime import datetime
from app.db.database import Base
from sqlalchemy import UniqueConstraint


class LeadHistory(Base):
    __tablename__ = "LEAD_HISTORY"
    __table_args__ = (
        UniqueConstraint("lead_id", "timestamp", name="uq_lead_timestamp"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, index=True)
    referal_page = Column(String, nullable=True)
    current_page = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    session_id = Column(String, nullable=True)
    user_id = Column(String, nullable=True)
    lead_id = Column(String, nullable=True)
    prop_id = Column(String, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    source = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)
    area = Column(String, nullable=True)
    name = Column(String, nullable=True)
    building_name = Column(String, nullable=True)
    furnishing_type = Column(String, nullable=True)
    unit_type = Column(String, nullable=True)

    # mobile_no = Column(String, nullable=True)
    # email_id = Column(String, nullable=True)

    