'''import os
import uuid
import tempfile
from hashlib import sha256
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, UploadFile, File, Body
from PIL import Image

from app.persistence.invoice_repository import InvoiceRepository
from app.db.database import SessionLocal
from app.services.ocr_engine import convert_to_images, run_ocr
from app.services.ocr_invoice_clip_validator import validate_invoice_image
from app.services.qwen_vl_invoice_service import QwenVLInvoiceService

router = APIRouter(prefix="/invoice", tags=["Invoice"])


# =====================================================
# OCR FALLBACK
# =====================================================
def run_rapidocr_fallback(images):
    texts = []

    for img_np in images:
        ocr_result = run_ocr(img_np)
        texts.append(ocr_result.get("text", ""))

    final_text = "\n\n".join([t for t in texts if t]).strip()

    return {
        "document_type": "invoice",
        "mode": "ocr_fallback",
        "plain_text": final_text,
    }


# =====================================================
# MERGE QWEN OUTPUT
# =====================================================
def merge_invoice_pages(page_results: list) -> dict:
    merged = {
        "vendor_name": None,
        "invoice_number": None,
        "invoice_date": None,
        "total_amount": None,
        "currency": None,
        "line_items": [],
    }

    for page in page_results:
        for key in merged.keys():
            if key != "line_items" and not merged[key]:
                merged[key] = page.get(key)

        if isinstance(page.get("line_items"), list):
            merged["line_items"].extend(page["line_items"])

    return merged


# =====================================================
# MAIN PROCESSOR
# =====================================================
def process_invoice_file(file_bytes: bytes, filename: str, source_url: str = None):
    file_hash = sha256(file_bytes).hexdigest()
    file_type = "pdf" if filename.lower().endswith(".pdf") else "image"

    images = convert_to_images(file_bytes, filename)

    if not images:
        return {"status": "failed", "error": "No images generated"}

    # STEP 1: CLIP VALIDATION
    #clip_result = validate_invoice_image(images[0], threshold=0.20)

    #if not clip_result.get("is_invoice"):
     #   return {
      #      "status": "rejected",
       #     "reason": "not_invoice",
        #    "file_hash": file_hash,
        #}

    # STEP 2: QWEN
    try:
        qwen = QwenVLInvoiceService.get_instance()
    except Exception:
        qwen = None

    page_results = []
    qwen_failed = False

    for i, img_np in enumerate(images, start=1):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
            temp_path = tmp.name

        Image.fromarray(img_np).save(temp_path)

        try:
            if qwen:
                result = qwen.extract_invoice_details(temp_path)
            else:
                result = {"parse_error": "Qwen not available"}
        finally:
            os.remove(temp_path)

        result["page_number"] = i
        page_results.append(result)

        if result.get("parse_error"):
            qwen_failed = True
            break

    # STEP 3: FALLBACK
    if qwen_failed:
        fallback = run_rapidocr_fallback(images)

        return {
            "status": "done",
            "mode": "ocr_fallback",
            "plain_text": fallback["plain_text"],
            "result": {
                "vendor_name": None,
                "invoice_number": None,
                "invoice_date": None,
                "total_amount": None,
                "currency": None,
            },
        }

    # STEP 4: SUCCESS
    merged = merge_invoice_pages(page_results)

    return {
        "status": "done",
        "mode": "qwen_structured",
        "plain_text": None,  # Qwen mode
        "result": merged,
    }


# =====================================================
# FILE UPLOAD API
# =====================================================
@router.post("/upload-files")
async def upload_invoice_files(files: list[UploadFile] = File(...)):
    db = SessionLocal()
    repo = InvoiceRepository(db)

    responses = []

    try:
        for file in files:
            try:
                file_bytes = await file.read()

                if not file_bytes:
                    responses.append({
                        "file": file.filename,
                        "status": "failed",
                        "error": "empty file"
                    })
                    continue

                file_hash = sha256(file_bytes).hexdigest()

                # ✅ DUPLICATE CHECK
                existing = repo.get_by_file_hash(file_hash)
                if existing:
                    responses.append({
                        "file": file.filename,
                        "status": "duplicate",
                        "invoice_id": str(existing.id)
                    })
                    continue

                # ✅ CREATE DB ROW
                row = repo.create_processing_entry(
                    file_name=file.filename,
                    #file_type="image",
                    file_hash=file_hash,
                    source="file"
                )

                # ✅ PROCESS
                result = process_invoice_file(file_bytes, file.filename)

                # ✅ SAVE
                repo.save_invoice_result(row.id, result)

                # ✅ FINAL RESPONSE
                responses.append({
                    "file": file.filename,
                    "invoice_id": str(row.id),
                    "status": "success",
                    "mode": result.get("mode"),

                    # 🔥 TOP LEVEL (as you wanted)
                    "plain_text": result.get("plain_text"),

                    "data": {
                        "vendor_name": result.get("result", {}).get("vendor_name"),
                        "invoice_number": result.get("result", {}).get("invoice_number"),
                        "invoice_date": result.get("result", {}).get("invoice_date"),
                        "total_amount": result.get("result", {}).get("total_amount"),
                        "currency": result.get("result", {}).get("currency"),
                    }
                })

            except Exception as e:
                responses.append({
                    "file": file.filename,
                    "status": "failed",
                    "error": str(e)
                })

    finally:
        db.close()

    return {
        "status": "completed",
        "count": len(files),
        "results": responses
    }


# =====================================================
# URL API
# =====================================================
@router.post("/process-urls")
async def process_invoice_urls(urls: list[str] = Body(...)):
    db = SessionLocal()
    repo = InvoiceRepository(db)

    responses = []

    try:
        for url in urls:
            try:
                response = requests.get(url, timeout=20)
                response.raise_for_status()

                file_bytes = response.content
                filename = urlparse(url).path.split("/")[-1] or f"{uuid.uuid4()}.jpg"
                file_hash = sha256(file_bytes).hexdigest()

                # DUPLICATE CHECK
                existing = repo.get_by_file_hash(file_hash)
                if existing:
                    responses.append({
                        "url": url,
                        "status": "duplicate",
                        "invoice_id": str(existing.id)
                    })
                    continue

                row = repo.create_processing_entry(
                    file_name=filename,
                    #file_type="image",
                    file_hash=file_hash,
                    source="url"
                )

                result = process_invoice_file(file_bytes, filename)

                repo.save_invoice_result(row.id, result)

                responses.append({
                    "url": url,
                    "invoice_id": str(row.id),
                    "status": "success",
                    "mode": result.get("mode"),
                    "plain_text": result.get("plain_text"),
                    "data": result.get("result")
                })

            except Exception as e:
                responses.append({
                    "url": url,
                    "status": "failed",
                    "error": str(e)
                })

    finally:
        db.close()

    return {
        "status": "completed",
        "count": len(urls),
        "results": responses
    }'''    













import uuid
from hashlib import sha256
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, UploadFile, File, Body

from app.persistence.invoice_repository import InvoiceRepository
from app.db.database import SessionLocal
from app.services.ocr_engine import convert_to_images, run_ocr

router = APIRouter(prefix="/invoice", tags=["Invoice"])


# =====================================================
# RAPIDOCR CORE
# =====================================================
def run_rapidocr(images):
    texts = []

    for img_np in images:
        try:
            ocr_result = run_ocr(img_np)
            texts.append(ocr_result.get("text", ""))
        except Exception as e:
            print(f"OCR page failed: {e}")

    final_text = "\n\n".join([t for t in texts if t]).strip()

    return final_text


# =====================================================
# OCR PROCESSOR
# =====================================================
def process_ocr_text(file_bytes: bytes, filename: str):
    images = convert_to_images(file_bytes, filename)

    if not images:
        return {
            "status": "failed",
            "mode": "ocr_only",
            "plain_text": ""
        }

    plain_text = run_rapidocr(images)

    return {
        "status": "done",
        "mode": "ocr_only",
        "plain_text": plain_text
    }


# backward compatibility
process_invoice_file = process_ocr_text


# =====================================================
# FILE UPLOAD API
# =====================================================
@router.post("/upload-files")
async def upload_invoice_files(files: list[UploadFile] = File(...)):
    db = SessionLocal()
    repo = InvoiceRepository(db)

    responses = []

    try:
        for file in files:
            try:
                file_bytes = await file.read()

                if not file_bytes:
                    responses.append({
                        "file": file.filename,
                        "status": "failed",
                        "error": "empty file"
                    })
                    continue

                file_hash = sha256(file_bytes).hexdigest()

                existing = repo.get_by_file_hash(file_hash)
                if existing:
                    responses.append({
                        "file": file.filename,
                        "status": "duplicate",
                        "invoice_id": str(existing.id)
                    })
                    continue

                row = repo.create_processing_entry(
                    file_name=file.filename,
                    file_hash=file_hash,
                    source="file"
                )

                result = process_ocr_text(file_bytes, file.filename)

                repo.save_invoice_result(row.id, result)

                responses.append({
                    "file": file.filename,
                    "invoice_id": str(row.id),
                    "status": "success",
                    "mode": result.get("mode"),
                    "plain_text": result.get("plain_text")
                })

            except Exception as e:
                responses.append({
                    "file": file.filename,
                    "status": "failed",
                    "error": str(e)
                })

    finally:
        db.close()

    return {
        "status": "completed",
        "count": len(files),
        "results": responses
    }


# =====================================================
# URL API
# =====================================================
@router.post("/process-urls")
async def process_invoice_urls(urls: list[str] = Body(...)):
    db = SessionLocal()
    repo = InvoiceRepository(db)

    responses = []

    try:
        for url in urls:
            try:
                response = requests.get(url, timeout=20)
                response.raise_for_status()

                file_bytes = response.content
                filename = urlparse(url).path.split("/")[-1] or f"{uuid.uuid4()}.jpg"
                file_hash = sha256(file_bytes).hexdigest()

                existing = repo.get_by_file_hash(file_hash)
                if existing:
                    responses.append({
                        "url": url,
                        "status": "duplicate",
                        "invoice_id": str(existing.id)
                    })
                    continue

                row = repo.create_processing_entry(
                    file_name=filename,
                    file_hash=file_hash,
                    source="url"
                )

                result = process_ocr_text(file_bytes, filename)

                repo.save_invoice_result(row.id, result)

                responses.append({
                    "url": url,
                    "invoice_id": str(row.id),
                    "status": "success",
                    "mode": result.get("mode"),
                    "plain_text": result.get("plain_text")
                })

            except Exception as e:
                responses.append({
                    "url": url,
                    "status": "failed",
                    "error": str(e)
                })

    finally:
        db.close()

    return {
        "status": "completed",
        "count": len(urls),
        "results": responses
    }    