import uuid
from datetime import datetime
from typing import Optional, List, Any

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, Float
from sqlalchemy.dialects.postgresql import UUID, JSONB

from app.db.database import Base


class OCRInvoiceResult(Base):
    __tablename__ = "ocr_invoice_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    source: Mapped[Optional[str]] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(20), default="processing")
    mode: Mapped[Optional[str]] = mapped_column(String(30))
    reason: Mapped[Optional[str]] = mapped_column(String(100))

    # 🔥 MUST HAVE
    plain_text: Mapped[Optional[str]] = mapped_column(Text)

    # 🔥 CORE INVOICE FIELDS
    # vendor_name: Mapped[Optional[str]] = mapped_column(String(255))
    # invoice_number: Mapped[Optional[str]] = mapped_column(String(100))
    # invoice_date: Mapped[Optional[str]] = mapped_column(String(50))

    # total_amount: Mapped[Optional[float]] = mapped_column(Float)
    # currency: Mapped[Optional[str]] = mapped_column(String(20))

    # 🔥 FLEXIBLE STORAGE
    # line_items: Mapped[List[Any]] = mapped_column(JSONB, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

