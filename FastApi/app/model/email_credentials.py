from sqlalchemy import Column, String, Integer, DateTime
from sqlalchemy.sql import func
from app.db.database import Base
class GmailAccount(Base):
    __tablename__ = "email_credentials"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False)
    refresh_token = Column(String, nullable=False)
    access_token = Column(String, nullable=True)
    token_expiry = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
