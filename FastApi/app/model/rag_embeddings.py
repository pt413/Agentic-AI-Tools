from sqlalchemy import Column, Integer, Text, TIMESTAMP, Boolean, func
from datetime import datetime
from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed
from sqlalchemy.dialects.postgresql import TSVECTOR
from app.db.database import Base


class RagEmbeddings(Base):
    __tablename__ = "rag_embeddings"

    id = Column(Integer, primary_key=True, index=True)

    source = Column(Text, nullable=False)
    source_id = Column(Text, nullable=False)

    chunks = Column(Text, nullable=False)

    type = Column(Integer, nullable=False, default=1)

    embedding = Column(Vector(384), nullable=False)
    bge_embedding = Column(Vector(768), nullable=True)

    
    is_processed = Column(Boolean, nullable=False, default=False, index=True)
    processed_at = Column(TIMESTAMP, nullable=True)

    # Existing timestamp
    updated_at = Column(
        TIMESTAMP,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )

    # Full-text search
    tsv = Column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(chunks, ''))",
            persisted=True
        )
    )