import json
import requests
from hashlib import sha256
from sqlalchemy import text

from app.db.database import SessionLocal
from app.persistence.invoice_repository import InvoiceRepository
#from app.routes.ocr_invoice_route import process_invoice_file

from app.routes.ocr_invoice_route import process_ocr_text
from app.services.whatsapp_image_type import classify_image_type_from_text
from app.services.whatsapp_ocr_payment import extract_payment_metadata

#ALLOWED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".pdf")



import os
from urllib.parse import urlparse, unquote

ALLOWED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".tiff", ".pdf")

CONTENT_TYPE_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/tiff": ".tiff",
    "application/pdf": ".pdf",
}


def filename_from_url(url: str, content_type: str = "") -> str:
    path = unquote(urlparse(url or "").path or "")
    filename = os.path.basename(path) or ""

    content_type = (content_type or "").split(";")[0].strip().lower()

    if not filename or "." not in filename:
        ext = CONTENT_TYPE_EXTENSION.get(content_type, ".jpg")
        return f"whatsapp_media{ext}"

    return filename



def process_whatsapp_ocr(message_id: int, url: str):
    db = SessionLocal()

    try:
        # ✅ STEP 1: validate


        '''if not url or not url.lower().endswith(ALLOWED_EXTENSIONS):
            print(f"⛔ Skipping non-image: {url}")

            db.execute(text("""
                UPDATE public.messages
                SET ocr_status = 'no_media'
                WHERE id = :id
            """), {"id": message_id})
            db.commit()

            return    

        print(f"📥 OCR started for message {message_id}")

        # ✅ STEP 2: mark processing
        db.execute(text("""
            UPDATE public.messages
            SET ocr_status = 'processing'
            WHERE id = :id
        """), {"id": message_id})
        db.commit()

        # ✅ STEP 3: download
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            raise Exception("Download failed")

        file_bytes = response.content
        filename = url.split("/")[-1] or "image.jpg"'''


        if not url:
            db.execute(text("""
                UPDATE public.messages
                SET ocr_status = 'no_media'
                WHERE id = :id
            """), {"id": message_id})
            db.commit()
            return

        print(f"📥 OCR started for message {message_id}")

        db.execute(text("""
            UPDATE public.messages
            SET ocr_status = 'processing'
            WHERE id = :id
        """), {"id": message_id})
        db.commit()

        response = requests.get(url, timeout=20)

        if response.status_code != 200:
            raise Exception(f"Download failed with status {response.status_code}")

        content_type = response.headers.get("content-type", "")
        filename = filename_from_url(url, content_type)

        ext = os.path.splitext(filename.lower())[1]

        if ext not in ALLOWED_EXTENSIONS:
            db.execute(text("""
                UPDATE public.messages
                SET ocr_status = 'no_media'
                WHERE id = :id
            """), {"id": message_id})
            db.commit()
            print(f"⛔ Unsupported media type: url={url}, content_type={content_type}, filename={filename}")
            return

        file_bytes = response.content
        file_hash = sha256(file_bytes).hexdigest()



        #file_hash = sha256(file_bytes).hexdigest()

        repo = InvoiceRepository(db)

        # ✅ STEP 4: skip duplicate OCR


        existing = repo.get_by_file_hash(file_hash)

        if existing:
            print("⏩ Already processed")

            '''safe_text = existing.plain_text if existing.plain_text else ""
            image_type = classify_image_type_from_text(safe_text)

            db.execute(text("""
                UPDATE public.messages
                SET 
                    ocr_status = 'done',
                    extracted_text = :text,
                    image_type = :image_type
                WHERE id = :id
            """), {
                "text": safe_text,
                "image_type": image_type,
                "id": message_id
            })
            db.commit()

            return'''  # ✅ VERY IMPORTANT



            safe_text = existing.plain_text if existing.plain_text else ""
            image_type = classify_image_type_from_text(safe_text)

            payment_metadata = {}

            if image_type == "payment_receipt":
                payment_metadata = extract_payment_metadata(safe_text)

            db.execute(text("""
                UPDATE public.messages
                SET 
                    ocr_status = 'done',
                    extracted_text = :text,
                    image_type = :image_type,
                    payment_metadata = CAST(:payment_metadata AS jsonb)
                WHERE id = :id
            """), {
                "text": safe_text,
                "image_type": image_type,
                #"payment_metadata": json.dumps(payment_metadata),
                "payment_metadata": json.dumps(payment_metadata, ensure_ascii=False),
                "id": message_id
            })
            db.commit()

            return



        # ✅ STEP 5: create OCR entry
        row = repo.create_processing_entry(
            file_name=filename,
            file_hash=file_hash,
            source=url
        )

        # ✅ STEP 6: run pipeline
        #result = process_invoice_file(file_bytes, filename)
        result = process_ocr_text(file_bytes, filename)

        # ✅ STEP 7: save OCR result
        repo.save_invoice_result(row.id, result)

        # ✅ STEP 8: extract plain_text

        plain_text = ""

        if isinstance(result, dict):
            plain_text = result.get("plain_text") or ""
        else:
            print("⚠️ Unexpected OCR result format")
        
        
        image_type = classify_image_type_from_text(plain_text)

        payment_metadata = {}

        if image_type == "payment_receipt":
            payment_metadata = extract_payment_metadata(plain_text)

        db.execute(text("""
            UPDATE public.messages
            SET 
                ocr_status = 'done',
                extracted_text = :text,
                image_type = :image_type,
                payment_metadata = CAST(:payment_metadata AS jsonb)
            WHERE id = :id
        """), {
            "text": plain_text,
            "image_type": image_type,
            #"payment_metadata": json.dumps(payment_metadata),
            "payment_metadata": json.dumps(payment_metadata, ensure_ascii=False),
            "id": message_id
        })    

        db.commit()

        print(f"✅ OCR completed for message {message_id}")

    except Exception as e:
        db.rollback()

        db.execute(text("""
            UPDATE public.messages
            SET ocr_status = 'failed'
            WHERE id = :id
        """), {"id": message_id})
        db.commit()

        print(f"❌ OCR failed for {message_id}: {e}")

    finally:
        db.close()