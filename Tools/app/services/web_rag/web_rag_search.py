from sentence_transformers.util import cos_sim as cosine_sim
from app.services.rag_search import (get_embed_model, get_rerank_model)
from app.services.web_rag.tavily_client import TavilySearchService
from app.services.web_rag.normalizer import normalize_tavily_results
import hashlib

def content_hash(text: str) -> str:
    return hashlib.md5(text.strip().lower().encode()).hexdigest()

def search_web_rag(query: str, top_k: int = 5):
    tavily = TavilySearchService()
    raw_results = tavily.search(
        query, 
        top_k, 
        include_domains = [
            "https://www.rentmystay.com/"
        ]
    )
    if not raw_results:
        return []
    chunks = normalize_tavily_results(raw_results)
    embed_model = get_embed_model()
    query_vec = embed_model.encode(
        query,
        convert_to_numpy=True,
        normalize_embeddings=True
    )
    for c in chunks:
        chunk_vec = embed_model.encode(
            c["chunks"],
            convert_to_numpy=True,
            normalize_embeddings=True
        )
        c["dense_score"] = float(cosine_sim(query_vec, chunk_vec))
    reranker = get_rerank_model()
    pairs = [(query, c["chunks"]) for c in chunks]
    scores = reranker.predict(pairs)
    ranked = list(zip(chunks, scores))
    ranked.sort(key=lambda x: x[1], reverse=True)
    seen = set()
    final = []
    for c, score in ranked:
        h = content_hash(c["chunks"])
        if h in seen:
            continue
        seen.add(h)
        final.append({
            "source": c["source"],
            "chunk": c["chunks"],
            "metadata": c["metadata"]
        })
        if len(final) == top_k:
            break
    return final


def universal_web_rag(query: str, top_k: int = 5):
    tavily = TavilySearchService()
    raw_results = tavily.search(
        query,
        top_k
    )
    if not raw_results:
        return []
    chunks = normalize_tavily_results(raw_results)
    embed_model = get_embed_model()
    query_vec = embed_model.encode(
        query,
        convert_to_numpy=True,
        normalize_embeddings=True
    )
    for c in chunks:
        chunk_vec = embed_model.encode(
            c["chunks"],
            convert_to_numpy=True,
            normalize_embeddings=True
        )
        c["dense_score"] = float(cosine_sim(query_vec, chunk_vec))
    reranker = get_rerank_model()
    pairs = [(query, c["chunks"]) for c in chunks]
    scores = reranker.predict(pairs)
    ranked = list(zip(chunks, scores))
    ranked.sort(key=lambda x: x[1], reverse=True)
    seen = set()
    final = []
    for c, score in ranked:
        h = content_hash(c["chunks"])
        if h in seen:
            continue
        seen.add(h)
        final.append({
            "source": c["source"],
            "chunk": c["chunks"],
            "metadata": c["metadata"]
        })
        if len(final) == top_k:
            break
    return final
