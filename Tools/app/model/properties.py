from sqlalchemy import Column, String, Integer, Float, TIMESTAMP, ForeignKey
from app.db.database import Base
from sqlalchemy.orm import relationship

class Property(Base):
    __tablename__ = "properties"

    prop_id = Column(String(50), primary_key=True, index=True)
    building_id = Column(String(50), ForeignKey("buildings.buid_id"), nullable=False)
    name = Column(String(200), nullable=False)
    unit = Column(String(50))
    unit_type = Column(String(50))
    bedrooms = Column(String(10))
    bathrooms = Column(String(10))
    max_guests = Column(Integer)
    advance = Column(Float)
    active = Column(Integer, nullable=False)
    agreement_date = Column(TIMESTAMP)
    furnishing_type = Column(String(50))
    daily_rent = Column(Float)
    monthly_rent = Column(Float)
    rms_rent = Column(Float)
    owner_name = Column(String(100))

    building = relationship(
        "Building",
        back_populates="properties",
    )
