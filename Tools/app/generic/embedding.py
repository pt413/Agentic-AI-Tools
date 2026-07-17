from sentence_transformers import SentenceTransformer
import numpy as np

# Load model once globally
_MODEL = SentenceTransformer("all-MiniLM-L6-v2")

async def generate_embedding(text: str):
    """
    Generate a normalized embedding for a given text string.
    """
    if not text:
        return np.zeros(384, dtype=float).tolist()
    
    vec = _MODEL.encode(text, show_progress_bar=False, convert_to_numpy=True)
    norm = np.linalg.norm(vec)
    if norm == 0:
        return vec.astype(float).tolist()
    return (vec / norm).astype(float).tolist()

async def intent_generate_embedding(text: str):
    """
    Generate a normalized embedding for a given text string.
    """
    if not text or not text.strip():
        return np.zeros(384, dtype=float).tolist()
    
    vec = _MODEL.encode(text, show_progress_bar=False, convert_to_numpy=True)
    norm = np.linalg.norm(vec)
    if norm == 0:
        return vec.astype(float).tolist()
    return (vec / norm).astype(float).tolist()

async def cosine_similarity(vec1, vec2):
    """
    Compute cosine similarity between two embeddings.
    """
    v1, v2 = np.array(vec1), np.array(vec2)
    if v1.shape != v2.shape or np.linalg.norm(v1) == 0 or np.linalg.norm(v2) == 0:
        return 0.0
    return float(np.dot(v1, v2))
