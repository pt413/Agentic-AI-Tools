import asyncio
from sqlalchemy.orm import Session
from app.db.database import SessionLocal
from app.model.message import Message
from app.generic.embedding import generate_embedding  


async def update_message_embeddings(db: Session):
    """Generate and store embeddings for ALL WhatsApp messages (loop through all batches)."""
    batch_size = 1000
    total_processed = 0

    try:
        while True:
            messages = db.query(Message).filter(
                Message.clean_content.isnot(None),
                # Message.clean_content_embedding.is_(None)
            ).limit(batch_size).all()

            if not messages:
                print(f"All embeddings processed! Total = {total_processed}")
                break

            print(f"Found {len(messages)} messages to embed (processed so far: {total_processed})")

            for msg in messages:
                try:
                    # msg.clean_content_embedding = generate_embedding(msg.clean_content)
                    pass
                except Exception as e:
                    print(f"Error embedding message ID {msg.id}: {e}")
                    continue

            db.commit()
            total_processed += len(messages)

            print(f"Stored embeddings for {len(messages)} messages (Total: {total_processed})")

    except Exception as e:
        print(f"Error updating message embeddings: {e}")
        db.rollback()
    finally:
        db.close()


async def run_message_embedding_updater():
    """Entrypoint for running the embedding updater."""
    db = SessionLocal()
    await update_message_embeddings(db)


if __name__ == "__main__":
    asyncio.run(run_message_embedding_updater())
