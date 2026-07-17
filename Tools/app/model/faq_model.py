from sqlalchemy import Column, Integer, Text, JSON, TIMESTAMP
from sqlalchemy.sql import func
from pgvector.sqlalchemy import Vector
from app.db.database import Base

class FAQ(Base):
    __tablename__ = "faq"

    id = Column(Integer, primary_key=True, index=True)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    faq_vector = Column(Vector(384))
