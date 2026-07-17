from sqlalchemy import Column, Integer, String, JSON, DateTime, func
from app.db.database import Base

class Intent(Base):
    __tablename__ = "intent"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True)  # e.g. "check booking details"
    example_query = Column(String, nullable=True)       # e.g. "Show me booking 52390"
    embedding = Column(JSON, nullable=False)            # vector embedding as JSON list
    created_at = Column(DateTime(timezone=True), server_default=func.now())
