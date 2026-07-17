import uuid
from typing import Optional, Any, Dict

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.schemas.ocr_invoice_schema import OCRInvoiceResult


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = value.replace(",", "").strip()
            if value == "":
                return None
        return float(value)
    except Exception:
        return None


class InvoiceRepository:
    def __init__(self, session: Session):
        self.session = session

    # =====================================================
    # GETTERS
    # =====================================================
    def get_by_file_hash(self, file_hash: str) -> Optional[OCRInvoiceResult]:
        return self.session.scalar(
            select(OCRInvoiceResult).where(OCRInvoiceResult.file_hash == file_hash)
        )

    def get_by_id(self, invoice_id: uuid.UUID) -> Optional[OCRInvoiceResult]:
        return self.session.scalar(
            select(OCRInvoiceResult).where(OCRInvoiceResult.id == invoice_id)
        )

    # =====================================================
    # CREATE ENTRY
    # =====================================================
    def create_processing_entry(
        self,
        file_name: str,
        file_hash: str,
        source: str | None = None,
    ) -> OCRInvoiceResult:

        row = OCRInvoiceResult(
            file_name=file_name,
            file_hash=file_hash,
            source=source, 
            status="processing",
            mode=None,
            reason=None,
            plain_text=None,

            #vendor_name=None,
            #invoice_number=None,
            #invoice_date=None,
            #total_amount=None,
            #currency=None,

            #line_items=[],
        )

        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    # =====================================================
    # SAVE FINAL RESULT
    # =====================================================
    def save_invoice_result(
        self,
        invoice_id: uuid.UUID,
        response_payload: Dict[str, Any],
    ):
        existing = self.get_by_id(invoice_id)
        if not existing:
            raise ValueError(f"Invoice row not found: {invoice_id}")

        result = response_payload.get("result") or {}

        values = {
            "status": response_payload.get("status", "done"),
            "mode": response_payload.get("mode"),
            "reason": response_payload.get("reason"),

            # 🔥 IMPORTANT
            "plain_text": response_payload.get("plain_text"),

            # 🔥 STRUCTURED FIELDS
            #"vendor_name": result.get("vendor_name"),
            #"invoice_number": result.get("invoice_number"),
            #"invoice_date": result.get("invoice_date"),

            #"total_amount": _to_float(result.get("total_amount")),
            #"currency": result.get("currency"),

            #"line_items": result.get("line_items") or [],
        }

        self.session.execute(
            update(OCRInvoiceResult)
            .where(OCRInvoiceResult.id == invoice_id)
            .values(**values)
        )

        self.session.commit()

    # =====================================================
    # MARK FAILED
    # =====================================================
    def mark_failed(
        self,
        invoice_id: uuid.UUID,
        error_message: str | None = None,
    ):
        self.session.execute(
            update(OCRInvoiceResult)
            .where(OCRInvoiceResult.id == invoice_id)
            .values(
                status="failed",
                reason=error_message[:100] if error_message else "failed",
            )
        )
        self.session.commit()