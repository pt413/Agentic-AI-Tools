from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime
from app.db.database import Base

class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(255), nullable=False)
    content_type = Column(String(100), nullable=False)
    user_id = Column(String(100), nullable=False, index=True)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    data = Column(JSONB)  # NeonDB friendly
