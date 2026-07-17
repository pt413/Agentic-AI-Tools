from sqlalchemy import Column, Integer, String, DateTime, func, JSON
from sqlalchemy.orm import relationship
from app.db.database import Base

class Organization(Base):
    __tablename__ = "organization"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    domain = Column(String, unique=True)
    hierarchy = Column(JSON, default=dict)  # e.g., {"CEO": ["VP", "Manager"]}
    details = Column(JSON, default=dict)    # e.g., {"industry": "Finance", "size": 500}
    # websearch_info = Column(JSON, default=dict)  # stored external data for RAG
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationship to users
    users = relationship("User", back_populates="organization")
