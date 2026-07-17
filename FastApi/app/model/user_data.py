from sqlalchemy import Column, Integer, String, DateTime, Text, Index, text
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.sql import func
from app.db.database import Base
from pgvector.sqlalchemy import Vector

class UserData(Base):
    __tablename__ = "user_data"

    u_id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    wa_num= Column(String, nullable=True)
    email= Column(String, nullable=True)
    role = Column(String, nullable=False, default="")
    creation_time = Column(DateTime(timezone=True), nullable=False)
    updation_time = Column(DateTime(timezone=True), nullable=False)
    # timestamp = Column(DateTime(timezone=True), nullable=False)
    org_id= Column(String, nullable=False, default='')
   