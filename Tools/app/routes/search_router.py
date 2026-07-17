# # # app/routes/search_router.py

# # from fastapi import APIRouter, Depends, HTTPException, Query, FastAPI
# # from fastapi.responses import PlainTextResponse
# # from sqlalchemy.orm import Session
# # from app.db.database import get_db
# # from app.model.scraped_page import ScrapedPage
# # from app.generic.scraped_gen import fetch_scraped_from_redis, load_scraped_to_redis
# # import numpy as np
# # from app.generic.embedding import generate_embedding

# # router = APIRouter(
# #     prefix="/api/search",
# #     tags=["search"]
# # )


# # @router.get("/", response_class=PlainTextResponse)
# # async def search_pages(
# #     query: str = Query(..., alias="q"),
# #     db: Session = Depends(get_db)
# # ):
# #     if query.startswith("http"):
# #         page = db.query(ScrapedPage).filter(ScrapedPage.url == query).first()
# #         if not page:
# #             raise HTTPException(status_code=404, detail="URL not found in database")
# #         return PlainTextResponse(content=f"{page.url}\n\n{page.content}")

# #     scraped_data = await fetch_scraped_from_redis()

# #     if not scraped_data:
# #         await load_scraped_to_redis()
# #         scraped_data = await fetch_scraped_from_redis()

# #     if not scraped_data:
# #         raise HTTPException(status_code=404, detail="No cached scraped data found in Redis or DB")

# #     candidate_chunks = []
# #     for page in scraped_data:
# #         chunk_text = page.get("chunk")
# #         emb = page.get("embedding")
# #         if not chunk_text or not emb:
# #             continue
# #         candidate_chunks.append({
# #             "url": page["url"],
# #             "chunk": chunk_text,
# #             "embedding": np.array(emb)
# #         })

# #     query_vec = np.array(generate_embedding(query))
# #     query_vec = query_vec / (np.linalg.norm(query_vec) + 1e-10)

# #     results = []
# #     for c in candidate_chunks:
# #         emb = c.get("embedding")
# #         if emb is None or len(emb) == 0:
# #             continue
# #         emb = emb / (np.linalg.norm(emb) + 1e-10)
# #         sim = np.dot(query_vec, emb)

# #         url = c["url"].lower()
# #         if any(word in url for word in query.lower().split()):
# #             sim += 0.05

# #         results.append({
# #             "url": c["url"],
# #             "chunk": c["chunk"],
# #             "similarity": sim
# #         })

# #     # ✅ helper
# #     def get_chunk_text(item):
# #         chunk = item.get("chunk", "")
# #         if isinstance(chunk, dict):
# #             chunk = chunk.get("text", "")
# #         return str(chunk).lower()

# #     # ✅ ensure query_words is declared before using
# #     query = query.strip().lower()
# #     query_words = set(query.split())

# #     # ✅ sort safely
# #     results = sorted(
# #         results,
# #         key=lambda x: sum(w in get_chunk_text(x) for w in query_words),
# #         reverse=True
# #     )

# #     # ✅ filter low-similarity matches
# #     results = [r for r in results if r["similarity"] > 0.2]
# #     if not results:
# #         return PlainTextResponse(content="No strong semantic match found. Try rephrasing your query.")

# #     # ✅ unique top 5 URLs
# #     seen_urls = set()
# #     unique_results = []
# #     for r in results:
# #         if r["url"] not in seen_urls:
# #             unique_results.append(r)
# #             seen_urls.add(r["url"])
# #         if len(unique_results) >= 5:
# #             break

# #     # ✅ format
# #     response_text = "\n\n---\n\n".join(
# #         f"{r['url']}\n\n{r['chunk']}\n\n(Similarity: {r['similarity']:.4f})"
# #         for r in unique_results
# #     )

# #     return PlainTextResponse(content=response_text)


# # if __name__ == "__main__":
# #     import uvicorn
# #     app = FastAPI(title="Standalone Search API")
# #     app.include_router(router)
# #     uvicorn.run(app, host="0.0.0.0", port=8002)




# # app/routes/search_router.py

# from fastapi import APIRouter, Depends, HTTPException, Query, FastAPI
# from fastapi.responses import PlainTextResponse
# from sqlalchemy.orm import Session
# from app.db.database import get_db
# from app.model.scraped_page import ScrapedPage
# from app.generic.scraped_gen import fetch_scraped_from_redis, load_scraped_to_redis
# from app.generic.embedding import generate_embedding
# import numpy as np

# router = APIRouter(
#     prefix="/api/search",
#     tags=["search"]
# )

# # -----------------------------------
# # 🧩 Helper: recursively flatten chunks
# # -----------------------------------
# def extract_text_from_chunk(chunk):
#     """Safely extract text from nested hierarchical chunk structures."""
#     if isinstance(chunk, str):
#         return chunk
#     elif isinstance(chunk, dict):
#         parts = []
#         if "text" in chunk:
#             parts.append(chunk["text"])
#         if "chunk" in chunk:
#             parts.append(extract_text_from_chunk(chunk["chunk"]))
#         if "subgroups" in chunk:
#             for sub in chunk["subgroups"]:
#                 parts.append(extract_text_from_chunk(sub))
#         return " ".join(parts)
#     elif isinstance(chunk, list):
#         return " ".join(extract_text_from_chunk(c) for c in chunk)
#     return ""


# @router.get("/", response_class=PlainTextResponse)
# async def search_pages(
#     query: str = Query(..., alias="q"),
#     db: Session = Depends(get_db)
# ):
#     # If direct URL passed → return its content
#     if query.startswith("http"):
#         page = db.query(ScrapedPage).filter(ScrapedPage.url == query).first()
#         if not page:
#             raise HTTPException(status_code=404, detail="URL not found in database")
#         return PlainTextResponse(content=f"{page.url}\n\n{page.content}")

#     # Load from Redis cache
#     scraped_data = await fetch_scraped_from_redis()
#     if not scraped_data:
#         await load_scraped_to_redis()
#         scraped_data = await fetch_scraped_from_redis()
#     if not scraped_data:
#         raise HTTPException(status_code=404, detail="No cached data found in Redis or DB")

#     # Prepare candidate chunks
#     candidate_chunks = []
#     for page in scraped_data:
#         raw_chunk = page.get("chunk")
#         chunk_text = extract_text_from_chunk(raw_chunk)
#         emb = page.get("embedding")
#         if chunk_text and emb:
#             print("Debug check → page keys:", page.keys())
#             print("Chunk preview:", str(raw_chunk)[:200])
#             print("Has embedding:", bool(emb))

#             candidate_chunks.append({
#                 "url": page["url"],
#                 "chunk": chunk_text.strip(),
#                 "embedding": np.array(emb)
#             })

#     if not candidate_chunks:
#         raise HTTPException(status_code=500, detail="No valid chunks available for search")

#     # -------------------------------
#     # 🧠 Generate query embedding
#     # -------------------------------
#     query_vec = np.array(generate_embedding(query))
#     query_vec = query_vec / (np.linalg.norm(query_vec) + 1e-10)

#     # Detect if query looks like a question (for contextual boosting)
#     is_question = any(query.lower().startswith(w) for w in [
#         "how", "what", "when", "where", "who", "why", "can", "does", "is"
#     ])

#     # -------------------------------
#     # 🧩 Compute cosine similarities + contextual boost
#     # -------------------------------
#     results = []
#     for c in candidate_chunks:
#         emb = c["embedding"]
#         emb = emb / (np.linalg.norm(emb) + 1e-10)
#         sim = float(np.dot(query_vec, emb))

#         text_lower = c["chunk"].lower()

#         # 🔹 Small context-based boosting
#         boost = 0.0
#         if is_question:
#             if any(x in text_lower for x in ["faq", "policy", "cancel", "refund", "how to", "terms"]):
#                 boost += 0.15  # stronger boost for FAQ / procedural info
#             if "book" in query.lower() and "booking" in text_lower:
#                 boost += 0.1
#         else:
#             if any(x in text_lower for x in ["property", "apartment", "rent", "room", "hsr", "marathahalli"]):
#                 boost += 0.05  # normal property-related content

#         adjusted_sim = min(sim + boost, 1.0)

#         results.append({
#             "url": c["url"],
#             "chunk": c["chunk"],
#             "similarity": adjusted_sim
#         })

#     # Filter and sort results
#     results = [r for r in results if r["similarity"] > 0.25]
#     results = sorted(results, key=lambda x: x["similarity"], reverse=True)[:5]

#     if not results:
#         return PlainTextResponse("No strong semantic match found. Try rephrasing your query.")

#     # Deduplicate URLs
#     seen = set()
#     final_results = []
#     for r in results:
#         if r["url"] not in seen:
#             final_results.append(r)
#             seen.add(r["url"])

#     # Format readable response
#     response_text = "\n\n---\n\n".join(
#         f"{r['url']}\n\n{r['chunk']}\n\n(Similarity: {r['similarity']:.4f})"
#         for r in final_results
#     )

#     return PlainTextResponse(content=response_text)


# # -------------------------------
# # 🚀 Standalone Test Runner
# # -------------------------------
# if __name__ == "__main__":
#     import uvicorn
#     app = FastAPI(title="Standalone Search API")
#     app.include_router(router)
#     uvicorn.run(app, host="0.0.0.0", port=8002)



# app/routes/search_router.py

from fastapi import APIRouter, Depends, HTTPException, Query, FastAPI
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.model.scraped_page import ScrapedPage
from app.generic.scraped_gen import fetch_scraped_from_redis, load_scraped_to_redis
import numpy as np
from app.generic.embedding import generate_embedding

router = APIRouter(
    prefix="/api/search",
    tags=["search"]
)

@router.get("/", response_class=PlainTextResponse)
async def search_pages(
    query: str = Query(..., alias="q"),
    db: Session = Depends(get_db)
):
    # If the query is a URL, fetch the page directly
    if query.startswith("http"):
        page = db.query(ScrapedPage).filter(ScrapedPage.url == query).first()
        if not page:
            raise HTTPException(status_code=404, detail="URL not found in database")
        return PlainTextResponse(content=f"{page.url}\n\n{page.content}")

    # Load scraped data from Redis
    scraped_data = await fetch_scraped_from_redis()

    # If Redis is empty, rebuild from Postgres once
    if not scraped_data:
        await load_scraped_to_redis()
        scraped_data = await fetch_scraped_from_redis()

    if not scraped_data:
        raise HTTPException(status_code=404, detail="No cached scraped data found in Redis or DB")

    # Prepare candidate chunks
    candidate_chunks = []
    for page in scraped_data:
        chunk_text = page.get("chunk")
        emb = page.get("embedding")
        if not chunk_text or not emb:
            continue
        candidate_chunks.append({
            "url": page["url"],
            "chunk": chunk_text,
            "embedding": np.array(emb)
        })

    # Generate normalized embedding for the input query
    query_vec = np.array(generate_embedding(query))
    query_vec = query_vec / (np.linalg.norm(query_vec) + 1e-10)

    results = []
    for c in candidate_chunks:
        emb = c.get("embedding")
        if emb is None or len(emb) == 0:
            continue
        emb = emb / (np.linalg.norm(emb) + 1e-10)
        sim = np.dot(query_vec, emb)
        results.append({
            "url": c["url"],
            "chunk": c["chunk"],
            "similarity": sim
        })

    # Sort by similarity and take top 20
    results = sorted(results, key=lambda x: x["similarity"], reverse=True)[:20]

    # Keep only first occurrence of each URL
    seen_urls = set()
    unique_results = []
    for r in results:
        if r["url"] not in seen_urls:
            unique_results.append(r)
            seen_urls.add(r["url"])
        if len(unique_results) >= 20:
            break

    # Format output
    response_text = "\n\n---\n\n".join(
        f"{r['url']}\n\n{r['chunk']}\n\n(Similarity: {r['similarity']:.4f})"
        for r in unique_results
    )

    return PlainTextResponse(content=response_text)


# -------------------------------
# 🚀 Standalone Test Runner
# -------------------------------
if __name__ == "__main__":
    import uvicorn
    app = FastAPI(title="Standalone Search API")
    app.include_router(router)
    uvicorn.run(app, host="0.0.0.0", port=8002)