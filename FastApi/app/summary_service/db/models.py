from sqlalchemy import Column, Integer, Text, DateTime, func
from .database import Base

class Summary(Base):
    __tablename__ = "summaries"

    id = Column(Integer, primary_key=True, index=True)
    source_type = Column(Text, nullable=False)   # email / whatsapp / call etc.
    source_id = Column(Text, nullable=False)     # unique ID from that system
    original_text = Column(Text, nullable=False)
    summary_text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
