from sqlalchemy import Column, String, Text, Integer, Float, TIMESTAMP
from app.db.database import Base
from sqlalchemy.orm import relationship

class Building(Base):
    __tablename__ = "buildings"

    buid_id = Column(String(50), primary_key=True, index=True)
    bname = Column(String(100), nullable=False)
    bcity = Column(String(50), nullable=False)
    barea = Column(String(100))
    baddress = Column(String(200))
    bpincode = Column(String(10))
    glat = Column(Float)
    glng = Column(Float)
    direction = Column(Text)
    status = Column(Integer, nullable=False)
    building_desc = Column(Text)
    caretaker = Column(String(100))
    superviser = Column(String(100))
    ops_manager = Column(String(100))
    rent_model = Column(String(50))
    agreement_date = Column(TIMESTAMP)
    sales = Column(String(100))
    sales_phone_no = Column(String(15))
    marketing = Column(String(100))
    updated_on = Column(TIMESTAMP)
    updated_by = Column(String(100))

    properties = relationship(
        "Property",
        back_populates="building",
        cascade="all, delete-orphan"
    )