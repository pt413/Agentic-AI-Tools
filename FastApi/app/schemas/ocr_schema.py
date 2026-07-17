import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, Boolean, Index, Float, LargeBinary, BigInteger
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.db.database import Base


class OCRResult(Base):
    """
    OCR Results storage with:
    - Deduplication
    - Structured extraction
    - Validation
    - Performance indexing
    """

    __tablename__ = "ocr_results"

    # =====================================================
    # PRIMARY KEY
    # =====================================================
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # =====================================================
    # FILE METADATA
    # =====================================================
    file_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True
    )

    file_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False  # image / pdf
    )

    file_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True
    )

    file_path: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True
    )


    # =====================================================
    # ORIGINAL UPLOADED FILE STORAGE IN DB
    # =====================================================
    file_bytes: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary,
        nullable=True
    )

    file_mime_type: Mapped[Optional[str]] = mapped_column(
        String(150),
        nullable=True
    )

    file_size: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True
    )

    # =====================================================
    # RAW OCR OUTPUT
    # =====================================================
    plain_text: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    raw_json: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False
    )

    # =====================================================
    # DOCUMENT CLASSIFICATION
    # =====================================================
    document_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="unknown",
        index=True
    )

    # =====================================================
    # STRUCTURED EXTRACTED DATA
    # =====================================================
    name: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True
    )

    id_number: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        index=True
    )

    dob: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
        index=True
    )

    
    gender: Mapped[Optional[str]] = mapped_column(
        String(10),
        nullable=True,
        index=True
    )

    phone: Mapped[Optional[str]] = mapped_column(
        String(15),
        nullable=True,
        index=True
    )

    address: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True
    )

    # =========================
    # NEW: ADDRESS COMPONENTS
    # =========================
    state: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        index=True
    )

    pincode: Mapped[Optional[str]] = mapped_column(
        String(10),
        nullable=True,
        index=True
    )

    is_valid_id: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        index=True
    )

    id_confidence: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        index=True
    )

    name_confidence: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        index=True
    )

    #CLIP SCORE
    # =========================
    clip_score: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        index=True
    )

    # =====================================================
    # PROCESSING STATUS
    # =====================================================
    status: Mapped[str] = mapped_column(
        String(20),
        default="done",
        index=True
    )

    # =====================================================
    # METADATA
    # =====================================================
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        index=True
    )

    # =====================================================
    # TABLE INDEXES (PostgreSQL Optimized)
    # =====================================================
    __table_args__ = (
        Index("idx_ocr_json_gin", "raw_json", postgresql_using="gin"),
        Index("idx_ocr_document_type_id", "document_type", "id_number"),
    )



# =====================================================
# REQUEST SCHEMA (API ONLY - NO DB IMPACT)
# =====================================================

from pydantic import BaseModel
from typing import Optional


class OCRUrlItem(BaseModel):
    id: Optional[int] = None
    link: str