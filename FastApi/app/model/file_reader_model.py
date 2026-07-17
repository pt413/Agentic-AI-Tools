# app/model/file.py
from sqlalchemy import Column, Integer, Text
from pgvector.sqlalchemy import Vector
from app.db.database import Base

class FileReader(Base):
    __tablename__ = "file_content"

    id = Column(Integer, primary_key=True, index=True)
    file_name = Column(Text)
    file_contact = Column(Text)
    file_vector = Column(Vector(384))
