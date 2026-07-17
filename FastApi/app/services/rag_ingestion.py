import re
import datetime
import uuid
from decimal import Decimal
from typing import List
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from sentence_transformers import SentenceTransformer
from app.model.rag_embeddings import RagEmbeddings
from app.model.message import Message
from app.model.emails import Email
from app.model.audio_file_model import AudioFile
from app.routes.files_rag import File
from app.model.faq_model import FAQ
from app.model.buildings import Building
from app.model.properties import Property
from sqlalchemy import func

_MODEL = None

def get_model():
    global _MODEL
    if _MODEL is None:
        _MODEL = SentenceTransformer("all-MiniLM-L6-v2")
    return _MODEL

SENTENCE_END_RE = re.compile(r"[.!?]")
def chunk_text(
    text: str,
    target_chars: int = 500,
    min_chunk_chars: int = 50,
) -> List[str]:
    if not text:
        return []
    text = re.sub(r"\s+", " ", text).strip()
    n = len(text)
    chunks = []
    cursor = 0
    while cursor < n:
        end = min(cursor + target_chars, n)
        if end < n and text[end].isalnum():
            while end < n and text[end].isalnum():
                end += 1
        match = SENTENCE_END_RE.search(text, end)
        if match:
            end = match.end()
        else:
            end = n
        chunk = text[cursor:end].strip()
        if len(chunk) >= min_chunk_chars:
            chunks.append(chunk)
        cursor = end
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

def ingest_buildings(db: Session):
    model = get_model()
    buildings = db.query(Building).all()
    total = 0
    existing_ids = set(
        r[0] for r in db.query(RagEmbeddings.source_id)
        .filter(RagEmbeddings.source == "buildings")
        .all()
    )
    for b in buildings:
        source_id = f"buildings_{b.buid_id}"
        if source_id in existing_ids:
            continue
        properties = db.query(Property).filter(
            Property.building_id == b.buid_id
        ).all()
        total_properties = len(properties)
        active_properties = len([p for p in properties if p.active == 1])
        rents = [p.monthly_rent for p in properties if p.monthly_rent]
        min_rent = min(rents) if rents else None
        max_rent = max(rents) if rents else None
        summary_text = f"""
            {b.bname}
            """
        embedding = model.encode(
            summary_text,
            convert_to_numpy=True,
            normalize_embeddings=True
        )
        db.add(
            RagEmbeddings(
                source="buildings",
                source_id=f"buildings_{b.buid_id}",
                chunks=summary_text.strip(),
                embedding=embedding.tolist(),
                updated_at=datetime.utcnow() + timedelta(hours=5, minutes=30),
                type=1
            )
        )
        total += 1
    db.commit()
    return {"buildings_ingested": total}

def build_property_text(p, building_map):
    b = building_map.get(p.building_id)
    building = b.bname if b else ""
    area = b.barea if b else ""
    city = b.bcity if b else ""
    return f"""
    This is a {p.unit_type or ""} apartment in {building}, located in {area}, {city}.
    The unit number is {p.unit or "not specified"}.
    The apartment is {p.furnishing_type or "not specified"}.
    It can accommodate up to {p.max_guests or "unknown"} guests.
    The monthly rent is {p.monthly_rent or "not specified"}.
    The owner is {p.owner_name or "not specified"}.
    """.strip()

def ingest_properties(db: Session):
    model = get_model()
    buildings = db.query(Building).all()
    building_map = {b.buid_id: b for b in buildings}
    existing_ids = fetch_ingested_row_ids(db, "properties", "properties")
    properties = db.query(Property).filter(Property.active == 1).all()
    batch = []
    batch_size = 500
    inserted = 0
    for p in properties:
        prop_id = str(p.prop_id)
        if prop_id in existing_ids:
            continue
        inserted+=1
        text = (
            f"Property Name: {p.name}. "
            f"{build_property_text(p, building_map)}"
        )
        embedding = model.encode(
            text,
            convert_to_numpy=True,
            normalize_embeddings=True
        )
        batch.append(
            RagEmbeddings(
                source="properties",
                source_id=f"properties_{prop_id}",
                chunks=text,
                embedding=embedding.tolist(),
                updated_at=datetime.utcnow() + timedelta(hours=5, minutes=30),
                type=1
            )
        )
        if len(batch) >= batch_size:
            db.bulk_save_objects(batch)
            db.commit()
            batch.clear()
    if batch:
        db.bulk_save_objects(batch)
        db.commit()
    return {"properties_ingested": inserted}

def ingest_table(
    db: Session,
    table_model,
    table_name: str,
    source: str,
    text_field: str,
    type_value: int
):
    ingested_row_ids = fetch_ingested_row_ids(db, table_name, source)
    rows = db.query(table_model).all()
    model = get_model()
    total_rows = 0
    total_chunks = 0
    batch_size = 500
    batch = []
    for row in rows:
        pk_column = list(row.__table__.primary_key.columns)[0].name
        row_id_value = getattr(row, pk_column)
        row_id_str = str(row_id_value)
        if row_id_str in ingested_row_ids:
            continue
        text = getattr(row, text_field, None)
        if not text:
            continue
        chunks = chunk_text(text)
        if not chunks:
            continue
        vectors = model.encode(
            chunks,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False
        )
        for chunk, emb in zip(chunks, vectors):
            batch.append(
                RagEmbeddings(
                    source=source,
                    source_id=f"{table_name}_{row.id}",
                    chunks=chunk,
                    embedding=emb.tolist(),
                    updated_at=datetime.utcnow() + timedelta(hours=5, minutes=30),
                    type=type_value
                )
            )
            total_chunks += 1
            if len(batch) >= batch_size:
                db.bulk_save_objects(batch)
                db.commit()
                batch.clear()
        total_rows += 1
    if batch:
        db.bulk_save_objects(batch)
        db.commit()
    return {
        "rows_ingested": total_rows,
        "chunks_ingested": total_chunks
    }

def ingest_all_tables(db: Session):
    summary = {}
    summary["messages"] = ingest_table(
        db, Message, "messages", "whatsapp", "clean_content", type_value=0
    )
    summary["emails"] = ingest_table(
        db, Email, "emails", "emails", "body", type_value=0
    )
    summary["calls"] = ingest_table(
        db, AudioFile, "call_recordings_transcript", "calls", "transcript_text", type_value=0
    )
    summary["files"] = ingest_table(
        db, File, "files", "files", "content", type_value=0
    )
    summary["buildings"] = ingest_buildings(
        db
    )
    summary["properties"] = ingest_properties(
        db
    )
    return summary
