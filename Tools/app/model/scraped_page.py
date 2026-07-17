# app/model/scraped_page.py
from sqlalchemy import Column, Integer, String, Text, DateTime
from datetime import datetime
from app.db.database import Base

class ScrapedPage(Base):
    __tablename__ = "WEB_URL_CONTENT"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, unique=True, index=True, nullable=False)
    content = Column(Text, nullable=True)          # plain text of page
    chunks = Column(Text, nullable=True)           # store JSON string of chunks
    embedding = Column(Text, nullable=True)        # store JSON string of embeddings
    last_synced = Column(DateTime, default=datetime.utcnow)  # new column for syncing timestamp
