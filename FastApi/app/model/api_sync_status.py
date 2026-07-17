from sqlalchemy import Column, Integer, DateTime
from app.db.database import Base
from datetime import datetime

class ApiSyncStatus(Base):
    __tablename__ = "api_sync_status"

    id = Column(Integer, primary_key=True, index=True)
    last_synced = Column(DateTime, default=datetime.utcnow, nullable=False)
