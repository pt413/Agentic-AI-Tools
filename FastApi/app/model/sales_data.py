from sqlalchemy import Column, Integer, String, Boolean
from app.db.database import Base

class SalesData(Base):
    __tablename__ = "sales_data"

    id = Column(Integer, primary_key=True, index=True)
    salesPhoneNumber = Column(String, nullable=False)
    username = Column(String, nullable=False)
    admin_Team = Column(String, nullable=True)
    distinct_ph = Column(String, nullable=True, default='')
    distinct_username= Column(String, nullable=True, default='')
    sync_status = Column(Integer, nullable=True, default='')
