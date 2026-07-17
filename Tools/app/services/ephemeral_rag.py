import json
import numpy as np
from typing import List, Any
from app.services.rag_search import get_embed_model, get_rerank_model

CACHE = {}

def normalize_data(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        texts = []
        for i, item in enumerate(data):
            if isinstance(item, dict):
                source = item.get("source", f"item_{i}")
                content = item.get("content", item)
                texts.append(
                    f"\n### SOURCE: {source}\n"
                    f"{json.dumps(content, indent=2)}\n"
                )
            else:
                texts.append(str(item))
        return "\n".join(texts)
    if isinstance(data, dict):
        return json.dumps(data, indent=2)
    return str(data)

def chunk_text(text: str, chunk_size: int = 500, overlap: int = 100):
    sentences = text.split("\n")
    chunks = []
    current_chunk = ""
    for sentence in sentences:
        if len(current_chunk) + len(sentence) < chunk_size:
            current_chunk += sentence + "\n"
        else:
            chunks.append(current_chunk)
            current_chunk = sentence + "\n"
    if current_chunk:
        chunks.append(current_chunk)
    return chunks

def cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

def search_in_data(
    query: str,
    data: Any,
    top_k: int = 5,
    rerank_multiplier: int = 4
):
    text = normalize_data(data)
    cache_key = hash(text)
    if cache_key in CACHE:
        chunks, chunk_embeddings = CACHE[cache_key]
    else:
        chunks = chunk_text(text)
        if not chunks:
            return []
        embed_model = get_embed_model()
        chunk_embeddings = embed_model.encode(
            chunks,
            convert_to_numpy=True,
            normalize_embeddings=True
        )
        CACHE[cache_key] = (chunks, chunk_embeddings)
    query_embedding = embed_model.encode(
        query,
        convert_to_numpy=True,
        normalize_embeddings=True
    )
    scores = np.dot(chunk_embeddings, query_embedding)
    candidate_k = min(len(chunks), top_k * rerank_multiplier)
    top_indices = np.argsort(scores)[::-1][:candidate_k]
    candidates = [(chunks[i], scores[i]) for i in top_indices]
    reranker = get_rerank_model()
    pairs = [(query, c[0]) for c in candidates]
    rerank_scores = reranker.predict(pairs)
    ranked = list(zip(candidates, rerank_scores))
    ranked.sort(key=lambda x: x[1], reverse=True)
    results = []
    for (chunk, sim_score), rerank_score in ranked[:top_k]:
        results.append({
            "chunk": chunk,
            "score": float(rerank_score)
        })
    return results