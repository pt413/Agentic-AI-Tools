#from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Body
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Body, Request, Response
import mimetypes
from hashlib import sha256
from typing import List
from sqlalchemy.orm import Session
import tempfile
import os
import uuid
import requests
from typing import Union, Dict, Any

from app.db.database import get_db, SessionLocal
from app.persistence.ocr_repository import OCRRepository
from app.services.ocr_engine import convert_to_images, run_ocr
from app.services.ocr_document_intelligence import extract_structured_data, evaluate_batch
from app.services.ocr_document_validator import validate_document

from app.schemas.ocr_schema import OCRUrlItem

router = APIRouter(prefix="/ocr", tags=["OCR"])


# =====================================================
# Utility: Save temp file
# =====================================================
def save_to_temp(file_bytes: bytes, original_name: str) -> str:
    suffix = os.path.splitext(original_name)[1] or ".jpg"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(file_bytes)
    tmp.close()
    return tmp.name




# This fixes the localhost issue.
def build_file_url(request: Request | None, ocr_id: uuid.UUID) -> str | None:
    public_base_url = os.getenv("PUBLIC_BASE_URL")

    if public_base_url:
        return public_base_url.rstrip("/") + f"/ocr/files/{ocr_id}"

    if request:
        return str(request.base_url).rstrip("/") + f"/ocr/files/{ocr_id}"

    return None




# =====================================================
# CORE OCR PROCESSING FUNCTION
# =====================================================
#def process_ocr_file(file_bytes: bytes, filename: str, repo: OCRRepository):

#def process_ocr_file(file_bytes: bytes, filename: str, repo: OCRRepository, source_url: str = None):
'''def process_ocr_file(
    file_bytes: bytes,
    filename: str,
    repo: OCRRepository,
    source_url: str = None,
    file_mime_type: str | None = None,
    request: Request | None = None,
):

    file_hash = sha256(file_bytes).hexdigest()
    #file_type = "pdf" if filename.lower().endswith(".pdf") else "image"

    ext = os.path.splitext(filename.lower())[1]

    if ext == ".pdf":
        file_type = "pdf"
    elif ext in [".doc", ".docx"]:
        file_type = "document"
    else:
        file_type = "image"

    existing = repo.get_by_file_hash(file_hash)
    if existing:

        if existing.document_type == "invalid_document":
            return {
                "ocr_id": str(existing.id),
                "status": "rejected",
                "is_valid_id": False,
                "reason": "Uploaded image is not a valid ID document",
                "clip_confidence": getattr(existing, "clip_score", None),
            }

        return {
            "ocr_id": str(existing.id),
            "status": "duplicate",
            "document_type": existing.document_type,
            "name": existing.name,
            "name_confidence": getattr(existing, "name_confidence", None),
            "id_number": existing.id_number,
            "id_confidence": getattr(existing, "id_confidence", None),
            "dob": existing.dob,
            "gender": getattr(existing, "gender", None),       # ✅ NEW
            "phone": getattr(existing, "phone", None),         # ✅ NEW
            "address": existing.address,
            "state": getattr(existing, "state", None),          # ✅ NEW
            "pincode": getattr(existing, "pincode", None),      # ✅ NEW
            "clip_score": getattr(existing, "clip_score", None),# ✅ NEW
            "is_valid_id": existing.is_valid_id,
            "plain_text": existing.plain_text,
        }

    temp_path = None
    ocr_row = None 

    try:


        images = convert_to_images(file_bytes, filename)

        valid_document = False
        clip_score = 0.0

        # =====================================================
        # DOCUMENT VALIDATION (CLIP)
        # =====================================================
        for img_np in images:
            is_doc, score = validate_document(img_np)
            #clip_score = score  # last score OR best score (can improve later)

            clip_score = max(clip_score, score)
            

        #for item in images:
            #original_img = item["original"]
            #is_doc, score = validate_document(original_img)    
            #clip_score = score  # last score OR best score (can improve later)
            #clip_score = max(clip_score, score)

            if is_doc:
                valid_document = True
                break


        if not valid_document:

            temp_path = save_to_temp(file_bytes, filename)
            stored_path = source_url if source_url else temp_path

            ocr_row = repo.create_processing_entry(
                file_name=filename,
                file_type=file_type,
                file_hash=file_hash,
                file_path=stored_path,
            )

            repo.save_ocr_result(
                ocr_id=ocr_row.id,
                plain_text="",
                raw_json={},
                document_type="invalid_document",
                structured_data={
                    "is_valid_id": False,
                    "clip_score": round(clip_score, 3),
                    "rejection_reason": "Uploaded image is not a valid ID document"
                },
            )

            return {
                "ocr_id": str(ocr_row.id),
                "status": "rejected",
                "is_valid_id": False,
                "reason": "Uploaded image is not a valid ID document",
                "clip_confidence": round(clip_score, 3)
            }
           

        # =====================================================
        # TEMP FILE
        # =====================================================
        temp_path = save_to_temp(file_bytes, filename)

        stored_path = source_url if source_url else temp_path

        ocr_row = repo.create_processing_entry(
            file_name=filename,
            file_type=file_type,
            file_hash=file_hash,
            file_path=stored_path,
        )

        texts = []
        raw_json = {}

        # =====================================================
        # OCR LOOP
        # =====================================================
        
        for i, img_np in enumerate(images):
            result = run_ocr(img_np)

        #for i, item in enumerate(images):
         #   processed_img = item["processed"]
          #  result = run_ocr(processed_img)

            texts.append(result["text"])
            raw_json[f"page_{i+1}"] = result["raw_json"]

        final_text = "\n\n".join(texts)

        # =====================================================
        # STRUCTURED EXTRACTION
        # =====================================================
        structured = extract_structured_data(
            final_text,
            image_path=temp_path,
            images=images
        )

        # =====================================================
        # ✅ ADD CLIP SCORE HERE (CRITICAL)
        # =====================================================
        structured["clip_score"] = round(clip_score, 3)

        # =====================================================
        # SAVE TO DB
        # =====================================================
        repo.save_ocr_result(
            ocr_id=ocr_row.id,
            plain_text=final_text,
            raw_json=raw_json,
            document_type=structured.get("document_type") or "unknown",
            structured_data=structured,
        )

        # =====================================================
        # CLEANUP
        # =====================================================
        #if os.path.exists(temp_path):
         #   os.remove(temp_path)

        # =====================================================
        # RESPONSE
        # =====================================================
        return {
            "ocr_id": str(ocr_row.id),
            "status": "done",
            "document_type": structured.get("document_type"),
            "name": structured.get("name"),
            "name_confidence": structured.get("name_confidence"),
            "id_number": structured.get("id_number"),
            "id_confidence": structured.get("id_confidence"),
            "dob": structured.get("dob"),
            "gender": structured.get("gender"),
            "phone": structured.get("phone"),
            "address": structured.get("address"),
            "state": structured.get("state"),           # ✅ NEW
            "pincode": structured.get("pincode"),       # ✅ NEW
            "clip_score": structured.get("clip_score"), # ✅ NEW
            "is_valid_id": structured.get("is_valid_id"),
            "plain_text": final_text,
        }

    except Exception as e:
   

        try:
            if ocr_row:
                repo.mark_failed(ocr_row.id)
        except:
            pass    

        return {
            "status": "failed",
            "is_valid_id": False,
            "error": str(e),
        }

    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)'''








def process_ocr_file(
    file_bytes: bytes,
    filename: str,
    repo: OCRRepository,
    source_url: str = None,
    file_mime_type: str | None = None,
    request: Request | None = None,
):
    file_hash = sha256(file_bytes).hexdigest()

    MAX_DB_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

    if len(file_bytes) > MAX_DB_FILE_SIZE:
        return {
            "status": "failed",
            "is_valid_id": False,
            "error": "File too large to store in database. Maximum allowed size is 10 MB.",
        }

    ext = os.path.splitext(filename.lower())[1]

    if ext == ".pdf":
        file_type = "pdf"
    elif ext in [".doc", ".docx"]:
        file_type = "document"
    else:
        file_type = "image"

    '''existing = repo.get_by_file_hash(file_hash)

    if existing:
        file_url = None

        if request:
            file_url = str(request.base_url).rstrip("/") + f"/ocr/files/{existing.id}"'''



    existing = repo.get_by_file_hash(file_hash)

    if existing:
        if not file_mime_type:
            file_mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        # If cleanup later removed file_bytes, restore them from this new upload.
        # This does not change OCR data.
        if not getattr(existing, "file_bytes", None):
            repo.restore_file_bytes(
                ocr_id=existing.id,
                file_bytes=file_bytes,
                file_mime_type=file_mime_type,
                file_size=len(file_bytes),
            )

        file_url = build_file_url(request, existing.id)



        if existing.document_type == "invalid_document":
            return {
                "ocr_id": str(existing.id),
                "status": "rejected",
                "is_valid_id": False,
                "reason": "Uploaded image is not a valid ID document",
                "clip_confidence": getattr(existing, "clip_score", None),
                "file_url": file_url,
            }

        return {
            "ocr_id": str(existing.id),
            "status": "duplicate",
            "document_type": existing.document_type,
            "name": existing.name,
            "name_confidence": getattr(existing, "name_confidence", None),
            "id_number": existing.id_number,
            "id_confidence": getattr(existing, "id_confidence", None),
            "dob": existing.dob,
            "gender": getattr(existing, "gender", None),
            "phone": getattr(existing, "phone", None),
            "address": existing.address,
            "state": getattr(existing, "state", None),
            "pincode": getattr(existing, "pincode", None),
            "clip_score": getattr(existing, "clip_score", None),
            "is_valid_id": existing.is_valid_id,
            "plain_text": existing.plain_text,
            "file_url": file_url,
        }

    temp_path = None
    ocr_row = None

    try:
        if not file_mime_type:
            file_mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        # =====================================================
        # CREATE DB ENTRY ONCE AT BEGINNING
        # Store original uploaded/downloaded file in DB
        # =====================================================
        ocr_row = repo.create_processing_entry(
            file_name=filename,
            file_type=file_type,
            file_hash=file_hash,
            file_path=source_url,  # optional: keep original external URL if available
            file_bytes=file_bytes,
            file_mime_type=file_mime_type,
            file_size=len(file_bytes),
        )

        '''file_url = None

        if request:
            file_url = str(request.base_url).rstrip("/") + f"/ocr/files/{ocr_row.id}"'''

        file_url = build_file_url(request, ocr_row.id)    

        images = convert_to_images(file_bytes, filename)

        valid_document = False
        clip_score = 0.0

        # =====================================================
        # DOCUMENT VALIDATION
        # =====================================================
        for img_np in images:
            is_doc, score = validate_document(img_np)
            clip_score = max(clip_score, score)

            if is_doc:
                valid_document = True
                break

        # =====================================================
        # REJECTED RESPONSE
        # DB row already has original file bytes
        # =====================================================
        if not valid_document:
            repo.save_ocr_result(
                ocr_id=ocr_row.id,
                plain_text="",
                raw_json={
                    "rejection_reason": "Uploaded image is not a valid ID document",
                    "clip_confidence": round(clip_score, 3),
                    "file_url": file_url,
                    "source_url": source_url,
                    "stored_in_db": True,
                    "file_size": len(file_bytes),
                    "file_mime_type": file_mime_type,
                },
                document_type="invalid_document",
                structured_data={
                    "is_valid_id": False,
                    "clip_score": round(clip_score, 3),
                },
            )

            return {
                "ocr_id": str(ocr_row.id),
                "status": "rejected",
                "is_valid_id": False,
                "reason": "Uploaded image is not a valid ID document",
                "clip_confidence": round(clip_score, 3),
                "file_url": file_url,
            }

        # =====================================================
        # TEMP FILE ONLY FOR OCR EXTRACTION
        # This is deleted in finally
        # =====================================================
        temp_path = save_to_temp(file_bytes, filename)

        texts = []
        raw_json = {}

        # =====================================================
        # OCR LOOP
        # =====================================================
        for i, img_np in enumerate(images):
            result = run_ocr(img_np)

            texts.append(result["text"])
            raw_json[f"page_{i + 1}"] = result["raw_json"]

        final_text = "\n\n".join(texts)

        # =====================================================
        # STRUCTURED EXTRACTION
        # =====================================================
        structured = extract_structured_data(
            final_text,
            image_path=temp_path,
            images=images,
        )

        structured["clip_score"] = round(clip_score, 3)


        # ADD FILE METADATA INTO RAW JSON FOR ACCEPTED/DONE FILES
        # =====================================================
        raw_json["_file"] = {
            "file_url": file_url,
            "source_url": source_url,
            "stored_in_db": True,
            "file_size": len(file_bytes),
            "file_mime_type": file_mime_type,
        }


        # =====================================================
        # SAVE OCR RESULT
        # =====================================================
        repo.save_ocr_result(
            ocr_id=ocr_row.id,
            plain_text=final_text,
            raw_json=raw_json,
            document_type=structured.get("document_type") or "unknown",
            structured_data=structured,
        )

        # =====================================================
        # ACCEPTED RESPONSE
        # =====================================================
        return {
            "ocr_id": str(ocr_row.id),
            "status": "done",
            "document_type": structured.get("document_type"),
            "name": structured.get("name"),
            "name_confidence": structured.get("name_confidence"),
            "id_number": structured.get("id_number"),
            "id_confidence": structured.get("id_confidence"),
            "dob": structured.get("dob"),
            "gender": structured.get("gender"),
            "phone": structured.get("phone"),
            "address": structured.get("address"),
            "state": structured.get("state"),
            "pincode": structured.get("pincode"),
            "clip_score": structured.get("clip_score"),
            "is_valid_id": structured.get("is_valid_id"),
            "plain_text": final_text,
            "file_url": file_url,
        }

    except Exception as e:
        try:
            if ocr_row:
                repo.mark_failed(ocr_row.id)
        except Exception:
            pass

        return {
            "status": "failed",
            "is_valid_id": False,
            "error": str(e),
        }

    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)           




# =====================================================
# 1️⃣ UPLOAD FILES
# =====================================================
'''@router.post("/upload-files")
async def upload_files(
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):'''

@router.post("/upload-files")
async def upload_files(
    request: Request,
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):

    results = []

    for file in files:

        try:

            file_bytes = await file.read()

            if not file_bytes:
                results.append({
                    "filename": file.filename,
                    "status": "failed",
                    "is_valid_id": False,
                    "error": "Empty file"
                })
                continue

            db_session = SessionLocal()

            try:

                repo = OCRRepository(db_session)

                #result = process_ocr_file(
                 #   file_bytes=file_bytes,
                  #  filename=file.filename,
                   # repo=repo, 
                    #source_url=None
                #)

                result = process_ocr_file(
                    file_bytes=file_bytes,
                    filename=file.filename,
                    repo=repo,
                    source_url=None,
                    file_mime_type=file.content_type,
                    request=request,
                )

            finally:
                db_session.close()

            result["source"] = "file"
            results.append(result)

        except Exception as e:

            results.append({
                "filename": file.filename,
                "status": "failed",
                "is_valid_id": False,
                "error": str(e),
            })

    summary = evaluate_batch(results)

    return {
        "status": "submitted",
        "total_files": len(files),
        "results": results,
        "summary": summary
    }


# =====================================================
# 2️⃣ PROCESS URLS
# =====================================================
from typing import Union, Dict, Any

'''@router.post("/process-urls")
async def process_urls(
    #urls: List[Union[str, Dict[str, Any]]] = Body(...),
    urls: List[Union[str, OCRUrlItem]] = Body(...),
    db: Session = Depends(get_db),
):'''

@router.post("/process-urls")
async def process_urls(
    request: Request,
    urls: List[Union[str, OCRUrlItem]] = Body(...),
    db: Session = Depends(get_db),
):

    results = []

    for item in urls:

        try:
            # ✅ HANDLE BOTH INPUT TYPES
            

            if isinstance(item, str):
                url = item
                external_id = None
            else:
                url = item.link
                external_id = item.id    

            #if not url:
             #   continue
            if not url:
                results.append({
                "id": external_id,
                "status": "failed",
                "is_valid_id": False,
                "error": "Missing URL"
                })
                continue

            # DOWNLOAD FILE
            response = requests.get(url, timeout=15)

            if response.status_code != 200:
                error_obj = {
                    "url": url,
                    "status": "failed",
                    "is_valid_id": False,
                    "error": f"HTTP {response.status_code}",
                }

                if external_id is not None:
                    error_obj["id"] = external_id

                results.append(error_obj)
                continue

            content_type = response.headers.get("Content-Type", "")


            content_type_base = content_type.split(";")[0].strip().lower()



            allowed_content_types = {
                "application/pdf",
                "application/msword",  # .doc
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
            }

            #if not (
             #   content_type.startswith("image/")
              #  or content_type in allowed_content_types
            #):
            
            if not (
                content_type_base.startswith("image/")
                or content_type_base in allowed_content_types
            ):

                error_obj = {
                    "url": url,
                    "status": "failed",
                    "is_valid_id": False,
                    "error": f"Unsupported content type: {content_type}",
                }

                if external_id is not None:
                    error_obj["id"] = external_id

                results.append(error_obj)
                continue

            file_bytes = response.content

            from urllib.parse import urlparse
            parsed_url = urlparse(url)
            filename = parsed_url.path.split("/")[-1] or f"{uuid.uuid4()}.jpg"

            # OCR PROCESS
            db_session = SessionLocal()

            try:
                repo = OCRRepository(db_session)

                '''result = process_ocr_file(
                    file_bytes=file_bytes,
                    filename=filename,
                    repo=repo,
                    source_url=url
                )'''


                result = process_ocr_file(
                    file_bytes=file_bytes,
                    filename=filename,
                    repo=repo,
                    source_url=url,
                    file_mime_type=content_type_base,
                    request=request,
                )



            finally:
                db_session.close()

            # RESPONSE FORMAT
            result["source"] = "url"
            result["url"] = url

            # ✅ OPTIONAL ID SUPPORT
            if external_id is not None:
                result["id"] = external_id

            results.append(result)

        except Exception as e:

            error_obj = {
                "url": url if 'url' in locals() else None,
                "status": "failed",
                "is_valid_id": False,
                "error": str(e),
            }

            if 'external_id' in locals() and external_id is not None:
                error_obj["id"] = external_id

            results.append(error_obj)

    summary = evaluate_batch(results)

    return {
        "status": "completed",
        "total_urls": len(urls),
        "results": results,
        "summary": summary
    }


# =====================================================
# 3️⃣ PROCESS TENANT PROOFS
# =====================================================
@router.get("/process-tenant-proofs")
async def process_tenant_proofs(
    type: str,
    offset: int = 0,
    limit: int = 10,
    db: Session = Depends(get_db),
):

    results = []

    external_api = (
        f"http://www.rentmystay.com/T/get_tenant_proofs"
        f"?type={type}&offset={offset}&limit={limit}"
    )

    try:

        headers = {"Authorization": "demo_token_123"}

        api_response = requests.get(
            external_api,
            headers=headers,
            timeout=20
        )

        if api_response.status_code != 200:
            raise HTTPException(
                status_code=500,
                detail=f"External API failed: {api_response.text}"
            )

        api_data = api_response.json()
        proofs = api_data.get("data", {}).get("results", [])

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    for proof in proofs:

        image_url = proof.get("s3_link")

        if not image_url:
            continue

        try:

            response = requests.get(image_url, timeout=15)

            if response.status_code != 200:
                results.append({
                    "url": image_url,
                    "status": "failed",
                    "is_valid_id": False,
                    "error": f"HTTP {response.status_code}"
                })
                continue

            file_bytes = response.content

            from urllib.parse import urlparse

            parsed_url = urlparse(image_url)
            filename = parsed_url.path.split("/")[-1] or "external.jpg"

            db_session = SessionLocal()

            try:

                repo = OCRRepository(db_session)

                result = process_ocr_file(
                    file_bytes=file_bytes,
                    filename=filename,
                    repo=repo,
                    source_url=image_url
                )

            finally:
                db_session.close()

            result["source"] = "external_api"
            result["url"] = image_url

            results.append(result)

        except Exception as e:

            results.append({
                "url": image_url,
                "status": "failed",
                "is_valid_id": False,
                "error": str(e)
            })

    summary = evaluate_batch(results)

    return {
        "status": "completed",
        "processed_count": len(results),
        "results": results,
        "summary": summary
    }    







# =====================================================
# 4️⃣ OPEN ORIGINAL OCR FILE FROM DB
# =====================================================
@router.get("/files/{ocr_id}")
def get_ocr_file(
    ocr_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    repo = OCRRepository(db)
    ocr = repo.get_by_id(ocr_id)

    if not ocr:
        raise HTTPException(status_code=404, detail="OCR record not found")

    if not getattr(ocr, "file_bytes", None):
        raise HTTPException(status_code=404, detail="File not found in database")
    #if not getattr(ocr, "file_bytes", None):
    #raise HTTPException(
     #   status_code=410,
     #   detail="Uploaded file was deleted after retention period. OCR data is still available."
    #)


    media_type = (
        getattr(ocr, "file_mime_type", None)
        or mimetypes.guess_type(ocr.file_name)[0]
        or "application/octet-stream"
    )

    safe_file_name = (ocr.file_name or "ocr-file").replace('"', "")

    headers = {
        "Content-Disposition": f'inline; filename="{safe_file_name}"'
    }

    return Response(
        content=ocr.file_bytes,
        media_type=media_type,
        headers=headers,
    )    




# =====================================================
# 5️⃣ DELETE ONLY OLD UPLOADED FILE BYTES FROM DB
# =====================================================
'''@router.delete("/files/cleanup-old")
def cleanup_old_uploaded_files(
    retention_days: int = 30,
    db: Session = Depends(get_db),
):
    repo = OCRRepository(db)

    cleared_count = repo.clear_old_uploaded_file_bytes(
        retention_days=retention_days
    )

    return {
        "status": "completed",
        "retention_days": retention_days,
        "cleared_file_count": cleared_count,
        "message": f"Deleted only uploaded file bytes older than {retention_days} days. OCR data was kept.",
    }    '''