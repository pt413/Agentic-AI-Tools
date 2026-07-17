import time
from sqlalchemy import text

from app.db.database import SessionLocal
from app.services.whatsapp_ocr_service import process_whatsapp_ocr


POLL_INTERVAL = 3
BATCH_SIZE = 50


def fetch_pending_rows():
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT id, r2_media_url
            FROM public.messages
            WHERE r2_media_url IS NOT NULL
              AND ocr_status = 'pending'
            ORDER BY id ASC
            LIMIT :limit
        """), {"limit": BATCH_SIZE}).fetchall()

        return rows

    finally:
        db.close()


def run_worker():
    print("🚀 WhatsApp OCR Worker Started...")

    while True:
        try:
            rows = fetch_pending_rows()

            if not rows:
                print("⏳ No pending messages...")
                time.sleep(POLL_INTERVAL)
                continue

            for row in rows:
                message_id = row[0]
                url = row[1]

                print(f"📥 Processing message {message_id}")

                try:
                    process_whatsapp_ocr(message_id, url)
                except Exception as e:
                    print(f"❌ Error processing message {message_id}: {e}")

        except Exception as e:
            print(f"❌ Worker loop error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run_worker()




'''import time
from sqlalchemy import text

from app.db.database import SessionLocal
from app.services.whatsapp_ocr_service import process_whatsapp_ocr


POLL_INTERVAL = 3        # seconds
BATCH_SIZE = 50          # how many messages per batch


def run_worker():
    print("🚀 WhatsApp OCR Worker Started...")

    while True:
        db = SessionLocal()

        try:
            # ✅ Fetch only pending image messages
            rows = db.execute(text("""
                SELECT id, r2_media_url
                FROM public.messages
                WHERE r2_media_url IS NOT NULL
                AND ocr_status = 'pending'
                ORDER BY id ASC
                LIMIT :limit
            """), {"limit": BATCH_SIZE}).fetchall()

            if not rows:
                print("⏳ No pending messages...")
                time.sleep(POLL_INTERVAL)
                continue

            for row in rows:
                message_id = row[0]
                url = row[1]

                print(f"📥 Processing message {message_id}")

                try:
                    # ✅ Single source of truth
                    process_whatsapp_ocr(message_id, url)

                except Exception as e:
                    print(f"❌ Error processing message {message_id}: {e}")

        finally:
            db.close()

        # small delay to avoid CPU overuse
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run_worker()'''


















'''import time
import requests
from hashlib import sha256
from sqlalchemy import text

from app.db.database import SessionLocal
from app.persistence.invoice_repository import InvoiceRepository
from app.routes.ocr_invoice_route import process_invoice_file


# ✅ Supported file types
ALLOWED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".pdf")


def is_valid_image(url: str) -> bool:
    if not url:
        return False
    return url.lower().endswith(ALLOWED_EXTENSIONS)


def run_worker():
    print("🚀 WhatsApp OCR Worker Started...")

    while True:
        db = SessionLocal()

        try:
            # ✅ Fetch limited recent messages
            rows = db.execute(text("""
                SELECT id, r2_media_url
                FROM public.messages
                WHERE r2_media_url IS NOT NULL
                ORDER BY id DESC
                LIMIT 300
                OFFSET 150
            """)).fetchall()

            if not rows:
                print("⏳ No messages found...")
                time.sleep(3)
                continue

            for row in rows:
                msg_id = row[0]
                url = row[1]

                # ✅ STEP 1: Filter only valid image URLs
                if not is_valid_image(url):
                    print(f"⛔ Skipping non-image: {url}")
                    continue

                try:
                    print(f"📥 Processing message {msg_id}")

                    # ✅ STEP 2: Download image
                    response = requests.get(url, timeout=15)

                    if response.status_code != 200:
                        print(f"❌ Failed to download: {url}")
                        continue

                    file_bytes = response.content

                    # fallback filename
                    filename = url.split("/")[-1] or "image.jpg"

                    # ✅ STEP 3: Generate hash
                    file_hash = sha256(file_bytes).hexdigest()

                    repo = InvoiceRepository(db)

                    # ✅ STEP 4: Skip duplicates
                    existing = repo.get_by_file_hash(file_hash)
                    if existing:
                        print(f"⏩ Skipped (already processed): {msg_id}")
                        continue

                    # ✅ STEP 5: Create DB entry (store URL as source)
                    row_obj = repo.create_processing_entry(
                        file_name=filename,
                        file_hash=file_hash,
                        source=url
                    )

                    # ✅ STEP 6: Run OCR pipeline
                    result = process_invoice_file(file_bytes, filename)
                    #result = process_ocr_text(file_bytes, filename)

                    # ✅ STEP 7: Save result
                    repo.save_invoice_result(row_obj.id, result)

                    print(f"✅ Processed message {msg_id}")

                except Exception as e:
                    print(f"❌ Error processing message {msg_id}: {e}")

        finally:
            db.close()

        # ✅ Polling interval (avoid CPU overuse)
        time.sleep(3)


if __name__ == "__main__":
    run_worker()'''







