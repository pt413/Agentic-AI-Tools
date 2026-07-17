from sqlalchemy import Column, Integer, String, DateTime, Text, Index, text, ForeignKey
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.sql import func
from app.db.database import Base
from pgvector.sqlalchemy import Vector

class UnifiedData(Base):
    __tablename__ = "uni_activity"

    activity_id = Column(Integer, primary_key=True, index=True)
    sen_id = Column(Integer, ForeignKey("user_data.u_id"), nullable=False)
    rec_id = Column(Integer, ForeignKey("user_data.u_id"), nullable=False)
    sender= Column(String, nullable=True)
    receiver= Column(String, nullable=True)
    channel = Column(String, nullable=True)
    content = Column(Text, nullable=False, default="")
    timestamp = Column(DateTime(timezone=True), nullable=False)
    meta_data = Column(JSON,nullable=True)
    embed_id = Column(Integer, nullable=True, default=0)
    # direction = Column(String, nullable=True)