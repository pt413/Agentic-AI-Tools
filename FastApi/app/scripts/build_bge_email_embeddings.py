import logging
from typing import List
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sentence_transformers import SentenceTransformer

from app.db.database import SessionLocal


EMBEDDING_MODEL_NAME = "BAAI/bge-base-en-v1.5"
BATCH_SIZE = 64
FETCH_LIMIT = 1000
MAX_TEXT_LENGTH = 2000

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bge_embedding_builder")


@contextmanager
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def load_model():
    logger.info("Loading BGE model: %s", EMBEDDING_MODEL_NAME)
    model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    return model


def fetch_rows(limit: int):
    """
    Fetch rows where embedding is missing (for ALL sources).
    """
    with get_db() as db:
        rows = db.execute(
            text("""
                SELECT id, chunks
                FROM rag_embeddings
                WHERE bge_embedding IS NULL
                LIMIT :limit
            """),
            {"limit": limit},
        ).fetchall()
    return rows


UPDATE_SQL = text("""
    UPDATE rag_embeddings
    SET bge_embedding = :embedding
    WHERE id = :id
""")


def update_batch(db, batch: List[dict]):
    for row in batch:
        db.execute(UPDATE_SQL, row)


def run():
    model = load_model()
    total_processed = 0

    while True:
        rows = fetch_rows(FETCH_LIMIT)

        if not rows:
            logger.info("No more chunks left to embed.")
            break

        logger.info("Fetched %d rows", len(rows))

        texts = []
        ids = []

        for row in rows:
            chunk = (row.chunks or "")[:MAX_TEXT_LENGTH]
            if chunk.strip():
                texts.append(chunk)
                ids.append(row.id)

        if not texts:
            logger.info("Skipping empty batch.")
            continue

        logger.info("Generating BGE embeddings...")
        embeddings = model.encode(
            texts,
            batch_size=BATCH_SIZE,
            normalize_embeddings=True,
            show_progress_bar=True,
        )

        with get_db() as db:
            try:
                batch_data = [
                    {
                        "id": ids[i],
                        "embedding": embeddings[i].tolist(),
                    }
                    for i in range(len(ids))
                ]

                update_batch(db, batch_data)
                db.commit()

                total_processed += len(batch_data)
                logger.info("Committed %d embeddings", len(batch_data))

            except SQLAlchemyError as exc:
                db.rollback()
                logger.error("Batch failed, rolled back: %s", exc)

    logger.info("Embedding complete. Total processed: %d", total_processed)


if __name__ == "__main__":
    run()