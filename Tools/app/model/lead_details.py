from sqlalchemy import Column, String, Text, DateTime, Integer, Index
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector
import datetime
from app.db.database import Base
from sqlalchemy import Boolean


class LeadActivity(Base):
    __tablename__ = "lead_activities_details"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    lead_id = Column(String(100), nullable=False, index=True)
    customer_phone = Column(String(20))
    customer_phone2 = Column(String(20))
    customer_email = Column(String(200))
    location = Column(String(200))
    origin = Column(String(100))
    status = Column(String(50), index=True)
    assigned_to = Column(String(100))
    added_by = Column(String(100))
    added_on = Column(DateTime)
    closed_on = Column(DateTime)
    last_updated_by = Column(String(100))
    followups = Column(JSONB, default=dict) 
    content = Column(Text, nullable=False)
    embedding = Column(Vector(384))  
    extracted_data = Column(JSONB, default=dict)
    activity_timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    
    # NEW: Add performance metrics reference
    performance_metrics_calculated = Column(Boolean, default=False)
    last_metrics_calculation = Column(DateTime)

# Existing indexes (keep your current indexes)
Index('idx_lead_activity_status', LeadActivity.status)
Index('idx_lead_activity_assigned', LeadActivity.assigned_to)