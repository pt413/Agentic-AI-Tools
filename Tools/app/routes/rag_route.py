from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.services.rag_ingestion import ingest_all_tables
from app.services.llm_answer import gen_llm_answer
# from app.schemas.rag_schema import RagSearchRequest, EphemeralRag
from app.schemas.rag_schema import RagSearchRequest
from app.services.rag_search import search_rag
# from app.services.ephemeral_rag import search_in_data
from app.services.web_rag.web_rag_search import search_web_rag, universal_web_rag

router = APIRouter(
    prefix="/rag",
    tags=["RAG"]
)

@router.post("/ingest")
def ingest_rag_data(db: Session = Depends(get_db)):
    """
    Trigger ingestion of all supported tables into RAG embeddings.
    """
    try:
        summary = ingest_all_tables(db)
        return {
            "status": "success",
            "message": "RAG ingestion completed",
            "details": summary
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"RAG ingestion failed: {str(e)}"
        )

@router.post("/search")
def rag_search(
    payload: RagSearchRequest,
    db: Session = Depends(get_db)
):
    if not payload.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")
    raw_results = search_rag(
        db=db,  
        query=payload.query,
        top_k=payload.top_k,
        source=payload.source
    )
    formatted_results = []
    all_results = []
    all_results.extend(raw_results.get("searchable_results", []))
    all_results.extend(raw_results.get("communication_results", []))
    for r in all_results:
        source_id = r.get("source_id")
        try:
            table, row_id = source_id.rsplit("_", 1)
        except ValueError:
            table = None
            row_id = source_id
        formatted_results.append({
            # "table": table,
            # "row_id": row_id,
            "source": r.get("source"),
            "chunks": r.get("chunk"),
            "score": r.get("score"),
            "metadata": r.get("metadata")
        })
    return {
        "query": payload.query,
        "results": formatted_results
    }

@router.post("/llm-answer")
def llm_answer(payload: RagSearchRequest, db: Session = Depends(get_db)):
    if not payload.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")
    result = gen_llm_answer(payload, db)
    return {
        "query": payload.query,
        "answer": result["answer"],
        "source": result["source"]
    }

@router.post("/web-search")
def web_rag_search(payload: RagSearchRequest):
    if not payload.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")
    results = search_web_rag(
        query = payload.query, 
        top_k = payload.top_k
    )
    return {
        "query": payload.query,
        "results": results
    }

@router.post("/universal-web-search")
def web_rag_search(payload: RagSearchRequest):
    if not payload.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")
    results = universal_web_rag(
        query = payload.query, 
        top_k = payload.top_k
    )
    return {
        "query": payload.query,
        "results": results
    }

# @router.post("/search-in-data")
# def rag_search_in_data(payload: EphemeralRag):
#     if not payload.query.strip():
#         raise HTTPException(status_code=400, detail="Query cannot be empty")
#     if not payload.data:
#         raise HTTPException(status_code=400, detail="Data is required")
#     results = search_in_data(
#         query=payload.query,
#         data=payload.data,
#         top_k=payload.top_k
#     )
#     return {
#         "query": payload.query,
#         "results": results
#     }