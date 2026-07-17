from fastapi import APIRouter
from pydantic import BaseModel
from pinecone import Pinecone
from sentence_transformers import SentenceTransformer
from dotenv import load_dotenv
import os
import time

load_dotenv()

router = APIRouter(
    prefix="/test/pinecone",
    tags=["Pinecone Test"]
)
pc = Pinecone(
    api_key=os.getenv("PINECONE_API_KEY"),
    environment="us-east-1-aws"
)
index = pc.Index("emails")

model = SentenceTransformer("all-MiniLM-L6-v2")

class QueryRequest(BaseModel):
    query: str
    top_k: int = 5

class MatchResponse(BaseModel):
    id: str
    score: float
    subject: str | None

class QueryResponse(BaseModel):
    query: str
    search_time_ms: float
    matches: list[MatchResponse]

@router.post("/search", response_model=QueryResponse)
def semantic_search(payload: QueryRequest):
    embedding = model.encode(payload.query).tolist()
    start = time.perf_counter()
    result = index.query(
        vector=embedding,
        top_k=payload.top_k,
        include_metadata=True
    )
    elapsed = (time.perf_counter() - start) * 1000
    matches = [
        MatchResponse(
            id=match.id,
            score=match.score,
            subject=match.metadata.get("subject")
        )
        for match in result.matches
    ]

    return QueryResponse(
        query=payload.query,
        search_time_ms=round(elapsed, 2),
        matches=matches
    )
