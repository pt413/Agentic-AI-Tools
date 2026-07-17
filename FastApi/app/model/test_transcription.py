from sqlalchemy import Column, Integer, String, DateTime, Text, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from app.db.database import Base
from pgvector.sqlalchemy import Vector

class TestTranscription(Base):
    __tablename__ = "test_transciption"

    id = Column(Integer, primary_key=True, index=True)
    eleven_labs = Column(Text, nullable=True)
    raw_format=Column(JSONB, nullable=True)
