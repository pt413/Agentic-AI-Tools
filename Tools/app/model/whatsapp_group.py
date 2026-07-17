from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime
from app.db.database import Base

class WhatsAppGroup(Base):
    __tablename__ = "whatsapp_groups"

    id = Column(String(255), primary_key=True, index=True) # group JID
    admin_number = Column(String(100), primary_key=True, index=True) # The admin who is part of this group
    subject = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    owner = Column(String(255), nullable=True) # JID of the owner
    creation = Column(DateTime, nullable=True)
    archived = Column(Boolean, default=False)
    pinned = Column(Boolean, default=False)
    last_sync = Column(DateTime, default=datetime.utcnow)
    
    # Relationship to participants
    participants = relationship("WhatsAppGroupParticipant", back_populates="group", cascade="all, delete-orphan")

class WhatsAppGroupParticipant(Base):
    __tablename__ = "whatsapp_group_participants"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(String(255), index=True)
    admin_number = Column(String(100), index=True)

    jid = Column(String(255), index=True, nullable=True)   # PN JID only
    lid = Column(String(255), index=True, nullable=True)   # LID JID only
    phone_number = Column(String(100), index=True, nullable=True)
    rank = Column(String(20), nullable=True)

    group = relationship("WhatsAppGroup", back_populates="participants")

    __table_args__ = (
        ForeignKeyConstraint(
            ["group_id", "admin_number"],
            ["whatsapp_groups.id", "whatsapp_groups.admin_number"],
            ondelete="CASCADE"
        ),
        UniqueConstraint("group_id", "admin_number", "jid", name="uq_group_participant_jid"),
        UniqueConstraint("group_id", "admin_number", "lid", name="uq_group_participant_lid"),
    )
    
