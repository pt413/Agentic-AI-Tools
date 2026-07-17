from sqlalchemy import Column, Integer, String, Text, DateTime
from datetime import datetime
from app.db.database import Base

class WebPage(Base):
    __tablename__ = "webpages"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String(500), unique=True, nullable=False, index=True)
    content = Column(Text, nullable=False)
    scraped_at = Column(DateTime, default=datetime.utcnow)
