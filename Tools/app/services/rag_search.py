from sqlalchemy.orm import Session
from sqlalchemy import func
from sentence_transformers import SentenceTransformer, CrossEncoder
from app.model.rag_embeddings import RagEmbeddings
from app.model.emails import Email
from app.model.message import Message
from app.model.audio_file_model import AudioFile
from app.model.faq_model import FAQ
from app.routes.files_rag import File
import datetime
import uuid
from decimal import Decimal
import hashlib
from sqlalchemy import inspect
from app.model.buildings import Building
from app.model.properties import Property

def content_hash(text: str) -> str:
    return hashlib.md5(text.strip().lower().encode()).hexdigest()

def make_json_safe(value):
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [make_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [make_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: make_json_safe(v) for k, v in value.items()}
    return str(value)

_EMBED_MODEL = None
_RERANK_MODEL = None
EXCLUDED_COLUMNS = {
    "emails": "body",
    "whatsapp": "clean_content",
    "calls": "transcript_text",
    "faq": "answer",
    "files": "content"
}

def get_embed_model():
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        _EMBED_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        # _EMBED_MODEL = SentenceTransformer("BAAI/bge-base-en-v1.5")
    return _EMBED_MODEL

def get_rerank_model():
    global _RERANK_MODEL
    if _RERANK_MODEL is None:
        _RERANK_MODEL = CrossEncoder(
            "cross-encoder/ms-marco-MiniLM-L-6-v2"
        )
    return _RERANK_MODEL

SOURCE_MODEL_MAP = {
    "emails": Email,
    "whatsapp": Message,
    "calls": AudioFile,
    "faq": FAQ,
    "files": File,
    "buildings": Building,
    "properties": Property,
}

def _parse_source_id(source_id: str):
    """
    Parse source_id of form:
        <table_name>_<row_id>

    Table names may contain underscores.
    """
    try:
        table, row_id = source_id.rsplit("_", 1)
        return table, row_id
    except (ValueError, AttributeError):
        return None

def _fetch_metadata(db: Session, source_id: str, excluded_column: str | None = None):
    parsed = _parse_source_id(source_id)
    if not parsed:
        return None
    table, row_id = parsed
    model = SOURCE_MODEL_MAP.get(table)
    if not model:
        return None
    pk_column = inspect(model).primary_key[0].name
    row = db.query(model).filter(getattr(model, pk_column) == row_id).first()
    if not row:
        return None
    metadata = {}
    for col in row.__table__.columns:
        if col.name == excluded_column:
            continue
        value = getattr(row, col.name)
        metadata[col.name] = make_json_safe(value)
    return metadata

def _search_by_type(
    db: Session,
    query: str,
    top_k: int = 5,
    source: str | None = None,
    alpha: float = 0.7,
    rerank_multiplier: int = 2,
    type_value: int = 1,
):
    """
    Production-grade Hybrid RAG Search
    Pipeline:
        Query
         → Dense + Sparse retrieval (pgvector + BM25)
         → Candidate expansion (top_k * rerank_multiplier)
         → Cross-encoder re-ranking
         → Metadata hydration
    """
    embed_model = get_embed_model()
    query_vector = embed_model.encode(
        query,
        convert_to_numpy=True,
        normalize_embeddings=True
    ).tolist()
    ts_query = func.plainto_tsquery("english", query)
    dense_score = 1 - RagEmbeddings.embedding.cosine_distance(query_vector)
    # dense_score = 1 - RagEmbeddings.bge_embedding.cosine_distance(query_vector)
    sparse_score = func.ts_rank_cd(RagEmbeddings.tsv, ts_query)
    hybrid_score = (
        alpha * dense_score
        + (1 - alpha) * sparse_score
    ).label("score")
    candidate_k = top_k * rerank_multiplier
    q = db.query(
        RagEmbeddings.source,
        RagEmbeddings.source_id,
        RagEmbeddings.chunks,
        hybrid_score
    )
    q = q.filter(RagEmbeddings.type == type_value)
    if(source == "string"):
        source = None
    if source:
        SOURCE_ALIASES = {
            "file": "files",
            "files": "files",
            "pdf": "files",
            "documents": "files",
            "call": "calls",
            "calls":"calls",
            "email":"emails",
            "emails":"emails",
            "whatsapp":"whatsapp",
            "faqs":"faq",
            "faq":"faq",
            "policy":"faq",
            "policies":"faq",
            "building": "buildings",
            "buildings": "buildings",
            "property": "properties",
            "properties": "properties",
        }
        source = SOURCE_ALIASES.get(source, source)
        q = q.filter(RagEmbeddings.source == source)
    q = (
        q
        # .filter(RagEmbeddings.tsv.op("@@")(ts_query))
        .order_by(hybrid_score.desc())
        .limit(candidate_k)
    )
    candidates = q.all()
    print(len(candidates))
    if not candidates:
        return []
    reranker = get_rerank_model()
    pairs = [
        (query, c.chunks)
        for c in candidates
    ]
    rerank_scores = reranker.predict(pairs)
    # rerank_scores = pairs
    ranked = list(zip(candidates, rerank_scores))
    ranked.sort(key=lambda x: x[1], reverse=True)
    seen = set()
    final = []
    for r, score in ranked:
        h = content_hash(" ".join(r.chunks.lower().split()))
        if h in seen:
            continue
        seen.add(h)
        final.append((r, score))
        if len(final) == top_k:
            break
    response = []
    for r, score in final:
        excluded_columns = EXCLUDED_COLUMNS.get(r.source)
        metadata = _fetch_metadata(db, r.source_id,excluded_columns)
        response.append({
            "source": r.source,
            "source_id": r.source_id,
            "chunk": r.chunks,
            "score": float(score),
            "metadata": metadata
        })
    return response

# def _search_by_type(
#     db: Session,
#     query: str,
#     top_k: int = 5,
#     source: str | None = None,
#     alpha: float = 0.7,
#     rerank_multiplier: int = 1,
#     type_value: int = 1,
# ):
#     """
#     Optimized RAG Search:
#         Query
#          → Vector retrieval (pgvector index)
#          → Candidate expansion
#          → Cross-encoder reranking (batched)
#          → Deduplication
#          → Metadata hydration
#     """
#     embed_model = get_embed_model()
#     query_vector = embed_model.encode(
#         query,
#         convert_to_numpy=True,
#         normalize_embeddings=True
#     ).tolist()
#     candidate_k = top_k * rerank_multiplier
#     query_base = db.query(
#         RagEmbeddings.source,
#         RagEmbeddings.source_id,
#         RagEmbeddings.chunks,
#     ).filter(RagEmbeddings.type == type_value)
#     if source == "string":
#         source = None
#     if source:
#         SOURCE_ALIASES = {
#             "file": "files",
#             "files": "files",
#             "pdf": "files",
#             "documents": "files",
#             "call": "calls",
#             "calls": "calls",
#             "email": "emails",
#             "emails": "emails",
#             "whatsapp": "whatsapp",
#             "faqs": "faq",
#             "faq": "faq",
#             "building": "buildings",
#             "buildings": "buildings",
#             "property": "properties",
#             "properties": "properties",
#         }
#         source = SOURCE_ALIASES.get(source, source)
#         query_base = query_base.filter(RagEmbeddings.source == source)
#     candidates = (
#         query_base
#         .order_by(RagEmbeddings.embedding.cosine_distance(query_vector))
#         .limit(candidate_k)
#         .all()
#     )
#     if not candidates:
#         return []
#     reranker = get_rerank_model()
#     pairs = [(query, c.chunks) for c in candidates]

#     rerank_scores = reranker.predict(
#         pairs,
#         batch_size=16  # 🚀 huge speedup
#     )
#     ranked = list(zip(candidates, rerank_scores))
#     ranked.sort(key=lambda x: x[1], reverse=True)
#     seen = set()
#     final = []
#     for r, score in ranked:
#         h = content_hash(" ".join(r.chunks.lower().split()))
#         if h in seen:
#             continue
#         seen.add(h)
#         final.append((r, score))
#         if len(final) == top_k:
#             break
#     response = []
#     for r, score in final:
#         excluded_columns = EXCLUDED_COLUMNS.get(r.source)
#         metadata = _fetch_metadata(
#             db,
#             r.source_id,
#             excluded_columns
#         )
#         response.append({
#             "source": r.source,
#             "source_id": r.source_id,
#             "chunk": r.chunks,
#             "score": float(score),
#             "metadata": metadata
#         })
#     return response

def search_rag(
    db: Session,
    query: str,
    top_k: int = 5,
    source: str | None = None,
    alpha: float = 0.7,
    rerank_multiplier: int = 4,
):
    """
    Returns separated results:
        - searchable_results (type=1)
        - communication_results (type=0)
    """
    if source in ["properties", "buildings", "faqs", "faq", "policy", "policies", "building", "property"]:
        return {
            "searchable_results" : _search_by_type(
                db=db,
                query=query,
                top_k=top_k,
                source=source,
                alpha=alpha,
                rerank_multiplier=rerank_multiplier,
                type_value=1
            )
        }

    else:
        return {
            "communication_results" : _search_by_type(
                db=db,
                query=query,
                top_k=top_k,
                source=source,
                alpha=alpha,
                rerank_multiplier=rerank_multiplier,
                type_value=0
            )
        }
