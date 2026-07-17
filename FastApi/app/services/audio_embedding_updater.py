import asyncio
from sqlalchemy.orm import Session
from app.db.database import SessionLocal
from app.model.audio_file_model import AudioFile
from app.generic.embedding import generate_embedding  


async def update_audio_embeddings(db: Session):
    """Generate and store embeddings for ALL audio transcripts (loop through all batches)."""
    batch_size = 500
    total_processed = 0

    try:
        while True:
            
            audios = db.query(AudioFile).filter(
                AudioFile.transcribed_text.isnot(None),
                AudioFile.transcript_embedding.is_(None)
            ).limit(batch_size).all()

            if not audios:
                print(f"All audio transcripts processed! Total = {total_processed}")
                break

            print(f"Found {len(audios)} transcripts to embed (processed so far: {total_processed})")

            for audio in audios:
                try:
                    audio.transcript_embedding = generate_embedding(audio.transcribed_text)
                except Exception as e:
                    print(f"Error embedding audio ID {audio.id}: {e}")
                    continue

            db.commit()
            total_processed += len(audios)

            print(f"Stored embeddings for {len(audios)} transcripts (Total: {total_processed})")

    except Exception as e:
        print(f"Error updating audio embeddings: {e}")
        db.rollback()
    finally:
        db.close()


async def run_audio_embedding_updater():
    """Entrypoint for running the audio embeddings updater."""
    db = SessionLocal()
    await update_audio_embeddings(db)


if __name__ == "__main__":
    asyncio.run(run_audio_embedding_updater())
