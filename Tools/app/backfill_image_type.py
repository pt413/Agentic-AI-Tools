



'''
import time
from sqlalchemy import text

from app.db.database import SessionLocal
from app.services.whatsapp_image_type import classify_image_type_from_text

BATCH_SIZE = 25   # keep smaller while debugging


def run_backfill():
    print("🚀 image_type backfill started...")
    db = SessionLocal()

    try:
        batch_no = 0

        while True:
            batch_no += 1
            print(f"\n📦 Fetching batch #{batch_no} ...")

            rows = db.execute(text("""
                SELECT id, extracted_text
                FROM public.messages
                WHERE r2_media_url IS NOT NULL
                  AND ocr_status = 'done'
                  AND image_type IS NULL
                ORDER BY id ASC
                LIMIT :limit
            """), {"limit": BATCH_SIZE}).fetchall()

            print(f"🔎 Batch #{batch_no} fetched {len(rows)} rows")

            if not rows:
                print("✅ image_type backfill complete")
                break

            for idx, row in enumerate(rows, start=1):
                msg_id = row[0]
                extracted_text = (row[1] or "").strip()

                print(f"➡️  [{batch_no}.{idx}] message_id={msg_id}")

                try:
                    print(f"   📝 text_len={len(extracted_text)}")

                    t1 = time.time()
                    image_type = classify_image_type_from_text(extracted_text)
                    t2 = time.time()

                    print(f"   🤖 classified={image_type} ({round(t2 - t1, 2)}s)")

                    db.execute(text("""
                        UPDATE public.messages
                        SET image_type = :image_type
                        WHERE id = :id
                    """), {
                        "image_type": image_type,
                        "id": msg_id
                    })

                    print(f"   💾 staged update for {msg_id}")

                except Exception as e:
                    print(f"   ❌ failed message_id={msg_id}: {e}")

            print(f"🟡 committing batch #{batch_no} ...")
            db.commit()
            print(f"✅ committed batch #{batch_no} ({len(rows)} rows)")

    except Exception as e:
        db.rollback()
        print(f"❌ backfill crashed: {e}")

    finally:
        db.close()
        print("🔒 DB closed")


if __name__ == "__main__":
    run_backfill()'''










import time
from sqlalchemy import text

from app.db.database import SessionLocal
from app.services.whatsapp_image_type import classify_image_type_from_text

BATCH_SIZE = 25


def fetch_rows(limit: int):
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT id, extracted_text
            FROM public.messages
            WHERE r2_media_url IS NOT NULL
              AND ocr_status = 'done'
              AND image_type IS NULL
            ORDER BY id ASC
            LIMIT :limit
        """), {"limit": limit}).fetchall()
        return rows
    finally:
        db.close()


def update_image_type(msg_id: int, image_type: str):
    db = SessionLocal()
    try:
        db.execute(text("""
            UPDATE public.messages
            SET image_type = :image_type
            WHERE id = :id
        """), {
            "image_type": image_type,
            "id": msg_id
        })
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def run_backfill():
    print("🚀 image_type backfill started...")
    batch_no = 0

    while True:
        batch_no += 1
        print(f"\n📦 Fetching batch #{batch_no} ...")

        rows = fetch_rows(BATCH_SIZE)
        print(f"🔎 Batch #{batch_no} fetched {len(rows)} rows")

        if not rows:
            print("✅ image_type backfill complete")
            break

        for idx, row in enumerate(rows, start=1):
            msg_id = row[0]
            extracted_text = (row[1] or "").strip()

            print(f"➡️  [{batch_no}.{idx}] message_id={msg_id}")
            print(f"   📝 text_len={len(extracted_text)}")

            try:
                t1 = time.time()
                image_type = classify_image_type_from_text(extracted_text)
                t2 = time.time()

                print(f"   🤖 classified={image_type} ({round(t2 - t1, 2)}s)")

                update_image_type(msg_id, image_type)

                print(f"   ✅ saved {msg_id}")

            except Exception as e:
                print(f"   ❌ failed message_id={msg_id}: {e}")


if __name__ == "__main__":
    run_backfill()    
