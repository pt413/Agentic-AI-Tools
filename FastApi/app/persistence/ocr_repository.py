import uuid
from typing import Optional, Dict, Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.schemas.ocr_schema import OCRResult

#from datetime import datetime, timedelta


class OCRRepository:
    def __init__(self, session: Session):
        self.session = session

    # GET BY FILE HASH (DEDUP)
    # =====================================================
    def get_by_file_hash(self, file_hash: str) -> Optional[OCRResult]:
        return self.session.scalar(
            select(OCRResult).where(OCRResult.file_hash == file_hash)
        )

    # CREATE PROCESSING ENTRY
    # =====================================================
    #def create_processing_entry(
     #   self,
      #  file_name: str,
       # file_type: str,
        #file_hash: str,
        #file_path: str | None = None,
    #) -> OCRResult:

    def create_processing_entry(
        self,
        file_name: str,
        file_type: str,
        file_hash: str,
        file_path: str | None = None,
        file_bytes: bytes | None = None,
        file_mime_type: str | None = None,
        file_size: int | None = None,
    ) -> OCRResult:

        ocr = OCRResult(
            file_name=file_name,
            file_type=file_type,
            file_hash=file_hash,
            file_path=file_path,

            # store actual file in DB
            file_bytes=file_bytes,
            file_mime_type=file_mime_type,
            file_size=file_size,

            status="processing",
            plain_text="",
            raw_json={},
            document_type="unknown",
            name=None,
            id_number=None,
            dob=None,
            address=None,
            is_valid_id=False,
        )

        self.session.add(ocr)
        self.session.commit()
        self.session.refresh(ocr)
        return ocr

    # SAVE OCR RESULT (UPDATED FOR STRUCTURED DATA)
    # =====================================================
    def save_ocr_result(
        self,
        ocr_id: uuid.UUID,
        plain_text: str,
        raw_json: dict,
        document_type: str,
        #structured_data: Dict[str, Optional[str]],
        structured_data: Dict[str, Any],
    ):


        existing = self.get_by_id(ocr_id)

        # FIX NUMPY TYPES BEFORE DB SAVE
        if structured_data.get("name_confidence") is not None:
            structured_data["name_confidence"] = float(structured_data["name_confidence"])

        if structured_data.get("id_confidence") is not None:
            structured_data["id_confidence"] = float(structured_data["id_confidence"])

        if structured_data.get("clip_score") is not None:
            structured_data["clip_score"] = float(structured_data["clip_score"])

        values = {
            "plain_text": plain_text,
            "raw_json": raw_json,
            "document_type": document_type,
            "status": "done",

            "name": structured_data.get("name") or existing.name,
            "id_number": structured_data.get("id_number") or existing.id_number,
            "dob": structured_data.get("dob") or existing.dob,
            "gender": structured_data.get("gender"),
            "phone": structured_data.get("phone"),

            # 🔥 CRITICAL FIX
            "address": structured_data.get("address") or existing.address,
            "state": structured_data.get("state") or existing.state,
            "pincode": structured_data.get("pincode") or existing.pincode,

            "clip_score": structured_data.get("clip_score"),
            "is_valid_id": structured_data.get("is_valid_id", False),
            "id_confidence": structured_data.get("id_confidence"),
            "name_confidence": structured_data.get("name_confidence"),
        }
        
        '''values = {
            "plain_text": plain_text,
            "raw_json": raw_json,
            "document_type": document_type,
            "status": "done",
            # Structured fields
            "name": structured_data.get("name"),
            "id_number": structured_data.get("id_number"),
            "dob": structured_data.get("dob"),
            "gender": structured_data.get("gender"),
            "phone": structured_data.get("phone"),

            "address": structured_data.get("address"),
            "state": structured_data.get("state"),
            "pincode": structured_data.get("pincode"),
            "clip_score": structured_data.get("clip_score"),
            "is_valid_id": structured_data.get("is_valid_id", False),
            "id_confidence": structured_data.get("id_confidence"),
            "name_confidence": structured_data.get("name_confidence"),
        }'''

        self.session.execute(
            update(OCRResult)
            .where(OCRResult.id == ocr_id)
            .values(**values)
        )

        self.session.commit()

    # MARK FAILED
    # =====================================================
    def mark_failed(self, ocr_id: uuid.UUID):
        self.session.execute(
            update(OCRResult)
            .where(OCRResult.id == ocr_id)
            .values(status="failed")
        )
        self.session.commit()
    
    

    # GET BY ID
    # =====================================================
    def get_by_id(self, ocr_id: uuid.UUID) -> Optional[OCRResult]:
        return self.session.scalar(
            select(OCRResult).where(OCRResult.id == ocr_id)
        )





    # RESTORE FILE BYTES FOR DUPLICATE AFTER CLEANUP
    # =====================================================
    def restore_file_bytes(
        self,
        ocr_id: uuid.UUID,
        file_bytes: bytes,
        file_mime_type: str | None,
        file_size: int,
    ):
        self.session.execute(
            update(OCRResult)
            .where(OCRResult.id == ocr_id)
            .values(
                file_bytes=file_bytes,
                file_mime_type=file_mime_type,
                file_size=file_size,
            )
        )

        self.session.commit()





    # OPTIONAL: SEARCH HELPERS (PRODUCTION READY)
    # =====================================================
    def get_by_id_number(self, id_number: str) -> Optional[OCRResult]:
        return self.session.scalar(
            select(OCRResult).where(OCRResult.id_number == id_number)
        )

    def get_by_document_type(self, document_type: str):
        return self.session.scalars(
            select(OCRResult).where(OCRResult.document_type == document_type)
        ).all()

    def get_valid_documents(self):
        return self.session.scalars(
            select(OCRResult).where(OCRResult.is_valid_id == True)
        ).all()

        



    # DELETE ONLY UPLOADED FILE BYTES AFTER RETENTION PERIOD
    # =====================================================
    def clear_old_uploaded_file_bytes(self, retention_days: int = 30) -> int:
        cutoff_date = datetime.utcnow() - timedelta(days=retention_days)

        result = self.session.execute(
            update(OCRResult)
            .where(OCRResult.created_at < cutoff_date)
            .where(OCRResult.file_bytes.isnot(None))
            .values(
                file_bytes=None,
            )
        )

        self.session.commit()

        return result.rowcount or 0
        
