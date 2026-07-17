from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, 
    Float, Numeric, JSON
)
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from pgvector.sqlalchemy import Vector
from app.db.database import Base  

class Booking(Base):
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(String, unique=True, index=True)

    booking_status = Column(String)
    primary_contact = Column(String)
    primary_email = Column(String)
    prop_id = Column(String)
    prop_name = Column(String)
    travel_from_date = Column(DateTime)
    travel_to_date = Column(DateTime)
    updated_at = Column(DateTime)
    
    # Additional fields from your JSON data
    user_id = Column(String)
    booking_type = Column(String)
    num_guests = Column(String)
    nights = Column(String)
    total_amount = Column(String)
    amount_paid = Column(String)
    advance_amount = Column(String)
    paid_advanced_amount = Column(String)
    booking_datetime = Column(DateTime)
    traveller_name = Column(String)
    
    # JSON fields for raw data
    booking_json = Column(JSON)
    
    # Vector embeddings
    booking_vector = Column(Vector(384))

    created_at = Column(DateTime, server_default=func.now())

    # Relationships (removed emails and whatsapps)
    invoices = relationship("Invoice", back_populates="booking", cascade="all, delete-orphan")
    calls = relationship("CallLogs", back_populates="booking", cascade="all, delete-orphan")
    tickets = relationship("Ticket", back_populates="booking", cascade="all, delete-orphan")
    communications = relationship("Communication", back_populates="booking", cascade="all, delete-orphan")


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(Integer, ForeignKey("bookings.id", ondelete="CASCADE"))
    
    invoice_id = Column(String, index=True)
    invoice_type = Column(String)
    invoice_from = Column(DateTime)
    invoice_to = Column(DateTime)
    received = Column(Numeric(15, 2))
    payable = Column(Numeric(15, 2))
    receipt_status = Column(String)
    duration = Column(String)
    invoice_status = Column(String)
    
    # JSON field for raw data
    invoice_json = Column(JSON)

    booking = relationship("Booking", back_populates="invoices")


class CallLogs(Base):
    __tablename__ = "call_logs"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(Integer, ForeignKey("bookings.id", ondelete="CASCADE"))

    username = Column(String)
    ph_num = Column(String)
    call_date = Column(DateTime)
    call_duration = Column(String)
    sales_phone_number = Column(String)
    call_type = Column(String)
    
    # JSON field for raw call data
    call_json = Column(JSON)
    call_vector = Column(Vector(384))

    booking = relationship("Booking", back_populates="calls")


class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(Integer, ForeignKey("bookings.id", ondelete="CASCADE"))

    ticket_id = Column(String, index=True)
    ticket_date = Column(DateTime)
    category = Column(String)
    description = Column(Text)
    prop_name = Column(String)
    status = Column(String)
    assign_to = Column(String)
    resolved_by = Column(String)
    
    # JSON field for raw ticket data
    ticket_json = Column(JSON)

    booking = relationship("Booking", back_populates="tickets")


class Communication(Base):
    __tablename__ = "communications"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(Integer, ForeignKey("bookings.id", ondelete="CASCADE"))

    type = Column(String)  # Comment, Agreement Audit, Check_in_out
    comment = Column(Text)
    added_by = Column(String)
    timestamp = Column(DateTime)
    
    # JSON field for raw communication data
    communication_json = Column(JSON)

    booking = relationship("Booking", back_populates="communications")