import re
from typing import List
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from sentence_transformers import SentenceTransformer
from app.model.rag_embeddings import RagEmbeddings
from app.model.faq_model import FAQ

_MODEL = None

def get_model():
    global _MODEL
    if _MODEL is None:
        _MODEL = SentenceTransformer("all-MiniLM-L6-v2")
    return _MODEL

def chunk_text(
    question: str,
    answer: str
) -> str:
    if not question or not answer:
        return None
    chunks = f"Question: {question} \n Answer: {answer}"
    return chunks

def fetch_ingested_row_ids(
    db: Session,
    table_name: str,
    source: str
) -> set:
    """
    Returns a set of row_ids already ingested for a table.
    Extracted from source_id = <table_name>_<row_id>
    """
    rows = (
        db.query(RagEmbeddings.source_id)
        .filter(
            RagEmbeddings.source == source,
            RagEmbeddings.source_id.like(f"{table_name}_%")
        )
        .all()
    )
    ingested_ids = set()
    for r in rows:
        try:
            _, row_id = r.source_id.split("_", 1)
            ingested_ids.add(row_id)
        except ValueError:
            continue
    return ingested_ids

def ingest_table(
    db: Session,
    table_model,
    table_name: str,
    source: str,
    ques: str,
    ans: str,
    type_value: int
):
    ingested_row_ids = fetch_ingested_row_ids(db, table_name, source)
    rows = db.query(table_model).all()
    model = get_model()
    total_rows = 0
    batch = []
    for row in rows:
        row_id_str = str(row.id)
        if row_id_str in ingested_row_ids:
            continue
        question = getattr(row, ques, None)
        answer = getattr(row, ans, None)
        if not question or not answer:
            continue
        chunk = chunk_text(question, answer)
        if not chunk:
            continue
        vectors = model.encode(
            chunk,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False
        )
        batch.append(
            RagEmbeddings(
                source=source,
                source_id=f"{table_name}_{row.id}",
                chunks=chunk,
                embedding=vectors.tolist(),
                updated_at=datetime.utcnow() + timedelta(hours= 5, minutes= 30),
                type=type_value
            )
        )
        total_rows += 1
    if batch:
        db.add_all(batch)
        db.commit()
    return {
        "rows_ingested": total_rows,
    }

def ingest_all_tables(db: Session):
    summary = {}
    summary["faqs"] = ingest_table(
        db, FAQ, "faqs", "faq", "question", "answer", type_value=1
    )
    return summary

if __name__ == "__main__":
    from app.db.database import SessionLocal
    db = SessionLocal()
    ingest_all_tables(db)
