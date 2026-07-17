import time
import requests
from sqlalchemy import text

from app.db.database import SessionLocal
from app.routes.ocr_invoice_route import process_invoice_file

BATCH_SIZE = 25
SLEEP_SECONDS = 2


def run_backfill():
    print("🚀 Backfill Empty OCR Text Worker Started...")

    while True:
        db = SessionLocal()

        try:
            rows = db.execute(text("""
                SELECT id, r2_media_url
                FROM public.messages
                WHERE r2_media_url IS NOT NULL
                  AND ocr_status = 'done'
                  AND (extracted_text IS NULL OR extracted_text = '')
                ORDER BY id ASC
                LIMIT :limit
            """), {"limit": BATCH_SIZE}).fetchall()

            if not rows:
                print("✅ Backfill complete. No empty OCR rows left.")
                break

            print(f"📦 Found {len(rows)} rows to reprocess...")

            for row in rows:
                message_id = row[0]
                url = row[1]

                print(f"📥 Reprocessing message {message_id}")

                try:
                    response = requests.get(url, timeout=15)
                    if response.status_code != 200:
                        print(f"❌ Download failed for message {message_id}: {url}")
                        continue

                    file_bytes = response.content
                    filename = url.split("/")[-1] or "image.jpg"

                    # Re-run OCR directly (bypasses duplicate invoice skip)
                    result = process_invoice_file(file_bytes, filename)

                    plain_text = ""
                    if isinstance(result, dict):
                        plain_text = result.get("plain_text") or ""

                    db.execute(text("""
                        UPDATE public.messages
                        SET extracted_text = :text
                        WHERE id = :id
                    """), {
                        "text": plain_text,
                        "id": message_id
                    })
                    db.commit()

                    print(f"✅ Updated message {message_id}")

                except Exception as e:
                    db.rollback()
                    print(f"❌ Failed message {message_id}: {e}")

        finally:
            db.close()

        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    run_backfill()