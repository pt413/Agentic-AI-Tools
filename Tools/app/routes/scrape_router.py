# # app/routes/scrape_router.py
# from datetime import datetime
# from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
# from pydantic import BaseModel
# from sqlalchemy.orm import Session
# from app.db.database import get_db
# from app.model.scraped_page import ScrapedPage

# from app.utils.scrape_chunker import smart_chunker
# from app.generic.embedding import _MODEL  # access model for batch encode
# from app.utils.rendered_scraper import get_rendered_page

# # Redis helpers
# from app.generic.scraped_gen import update_single_page_in_redis, fetch_scraped_from_redis, load_scraped_to_redis

# import httpx
# import json
# import asyncio
# import numpy as np
# import re
# import logging

# # -----------------------------
# # Pydantic schemas
# # -----------------------------
# class UrlRequest(BaseModel):
#     url: str

# class UrlListRequest(BaseModel):
#     urls: list[str]

# class ScrapedPageResponse(BaseModel):
#     url: str
#     content: str

# # ✅ Added proper output schema to avoid FastAPI serialization error
# class ScrapedPageOut(BaseModel):
#     url: str
#     content: str

# # -----------------------------
# # FastAPI router
# # -----------------------------
# router = APIRouter(
#     prefix="/api/scrape",
#     tags=["scrape"]
# )

# logger = logging.getLogger("scraper")

# # -----------------------------
# # Utility: Normalize URL
# # -----------------------------
# def normalize_url(url: str) -> str:
#     if not url.startswith(("http://", "https://")):
#         return "https://" + url
#     return url

# # -----------------------------
# # Text_cleaner: To remove repetitions in scraped text 
# # -----------------------------
# def clean_repetitions(text: str) -> str:
#     """Remove consecutive repeated lines or repeated phrases in the output."""
#     lines = text.splitlines()
#     cleaned = []
#     for line in lines:
#         if not cleaned or line.strip() != cleaned[-1].strip():
#             cleaned.append(line)
#     text = "\n".join(cleaned)
#     text = re.sub(r'(\b[\w\s]{3,20}\b)(\s*\1\b)+', r'\1', text, flags=re.IGNORECASE)
#     text = re.sub(r'[-=]{3,}', '---', text)
#     return text.strip()

# # -----------------------------
# # Background task: Generate embeddings and refresh Redis
# # -----------------------------
# def generate_and_store_embeddings(page_id: int, chunks: list, db: Session):
#     embeddings = _MODEL.encode(chunks, show_progress_bar=False, convert_to_numpy=True).astype(float).tolist()
#     page = db.query(ScrapedPage).filter(ScrapedPage.id == page_id).first()
#     if page:
#         page.chunks = json.dumps(chunks)
#         page.embedding = json.dumps(embeddings)
#         db.commit()
#         try:
#             asyncio.run(update_single_page_in_redis(page_id))
#         except Exception as e:
#             print(f"⚠️ Failed to update Redis for page {page_id}: {e}")

# # -----------------------------
# # POST endpoint: Scrape a single URL
# # -----------------------------
# @router.post("/")
# async def scrape_single(request: UrlRequest, db: Session = Depends(get_db)):
#     """Scrape a single page and store it."""
#     url = request.url  # ✅ Extract from JSON body
#     try:
#         text_content = await get_rendered_page(url)
#         if not text_content:
#             raise HTTPException(status_code=400, detail="Empty content returned from scraper")

#         chunks = smart_chunker(text_content)
#         for chunk in chunks:
#             embedding = _MODEL.encode(chunk, show_progress_bar=False, convert_to_numpy=True).tolist()
#             db.add(ScrapedPage(url=url, chunk=chunk, embedding=embedding))
#         db.commit()

#         await load_scraped_to_redis()
#         logger.info(f"✅ Updated page {url} in Redis")
#         return {"status": "success", "url": url, "chunks": len(chunks)}

#     except Exception as e:
#         logger.error(f"❌ Error scraping {url}: {e}")
#         raise HTTPException(status_code=500, detail=str(e))

# # -----------------------------
# # ✅ FIXED endpoint: Retrieve scraped page by URL
# # -----------------------------
# @router.get("/by_url", response_model=ScrapedPageOut)
# def get_scraped_page(url: str, db: Session = Depends(get_db)):
#     """Retrieve scraped page by URL: first try Redis, fallback to Postgres"""
#     content = fetch_scraped_from_redis(url)
#     if content:
#         return ScrapedPageOut(url=normalize_url(url), content=content)

#     page = db.query(ScrapedPage).filter(ScrapedPage.url == normalize_url(url)).first()
#     if not page:
#         raise HTTPException(status_code=404, detail="Page not found")
#     return ScrapedPageOut(url=page.url, content=page.content)

# # -----------------------------
# # POST endpoint: Scrape multiple URLs
# # -----------------------------
# @router.post("/batch")
# async def scrape_batch(urls: list[str], db: Session = Depends(get_db)):
#     """Scrape multiple URLs concurrently and store results."""
#     if not urls:
#         raise HTTPException(status_code=400, detail="URL list cannot be empty")

#     results = []

#     async def process_url(url):
#         try:
#             text_content = await get_rendered_page(url)
#             if not text_content:
#                 return {"url": url, "status": "failed", "reason": "Empty content"}

#             chunks = smart_chunker(text_content)
#             for chunk in chunks:
#                 embedding = _MODEL.encode(chunk, show_progress_bar=False, convert_to_numpy=True).tolist()
#                 db.add(ScrapedPage(url=url, chunk=chunk, embedding=embedding))
#             db.commit()
#             return {"url": url, "status": "success", "chunks": len(chunks)}
#         except Exception as e:
#             logger.error(f"❌ Error scraping {url}: {e}")
#             return {"url": url, "status": "failed", "reason": str(e)}

#     sem = asyncio.Semaphore(3)

#     async def bounded_process(url):
#         async with sem:
#             return await process_url(url)

#     scraped_data = await asyncio.gather(*(bounded_process(u) for u in urls))
#     await load_scraped_to_redis()

#     return {"results": scraped_data, "count": len(scraped_data)}





# # # app/routes/scrape_router.py
# # from datetime import datetime
# # from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
# # from pydantic import BaseModel
# # from sqlalchemy.orm import Session
# # from app.db.database import get_db
# # from app.model.scraped_page import ScrapedPage

# # from app.utils.scrape_chunker import chunk_text  # <-- use hierarchical chunker
# # from app.generic.embedding import _MODEL  # access model for batch encode
# # from app.utils.rendered_scraper import get_rendered_page

# # # Redis helpers
# # from app.generic.scraped_gen import update_single_page_in_redis, fetch_scraped_from_redis

# # import httpx
# # import json
# # import asyncio
# # import re

# # # -----------------------------
# # # Pydantic schemas
# # # -----------------------------
# # class UrlRequest(BaseModel):
# #     url: str

# # class UrlListRequest(BaseModel):
# #     urls: list[str]

# # class ScrapedPageResponse(BaseModel):
# #     url: str
# #     content: str

# # # -----------------------------
# # # FastAPI router
# # # -----------------------------
# # router = APIRouter(
# #     prefix="/api/scrape",
# #     tags=["scrape"]
# # )

# # # -----------------------------
# # # Utility: Normalize URL
# # # -----------------------------
# # def normalize_url(url: str) -> str:
# #     if not url.startswith(("http://", "https://")):
# #         return "https://" + url
# #     return url

# # # -----------------------------
# # # Text_cleaner: To remove repetitions in scraped text 
# # # -----------------------------
# # def clean_repetitions(text: str) -> str:
# #     # Remove consecutive duplicate lines
# #     lines = text.splitlines()
# #     cleaned = []
# #     for line in lines:
# #         if not cleaned or line.strip() != cleaned[-1].strip():
# #             cleaned.append(line)
# #     text = "\n".join(cleaned)

# #     # Remove very short phrase repetitions
# #     text = re.sub(r'(\b[\w\s]{3,20}\b)(\s*\1\b)+', r'\1', text, flags=re.IGNORECASE)
# #     # Remove excessive dashes
# #     text = re.sub(r'[-=]{3,}', '---', text)

# #     return text.strip()

# # # -----------------------------
# # # Background task: Generate embeddings and refresh Redis once
# # # -----------------------------
# # def generate_and_store_embeddings(page_id: int, chunks: list, db: Session):
# #     embeddings = _MODEL.encode(chunks, show_progress_bar=False, convert_to_numpy=True).astype(float).tolist()
# #     page = db.query(ScrapedPage).filter(ScrapedPage.id == page_id).first()
# #     if page:
# #         page.chunks = json.dumps(chunks)
# #         page.embedding = json.dumps(embeddings)
# #         db.commit()

# #         # Refresh only new data to Redis cache only once here
# #         try:
# #             asyncio.run(update_single_page_in_redis(page_id))
# #         except Exception as e:
# #             print(f"⚠️ Failed to update Redis for page {page_id}: {e}")

# # # -----------------------------
# # # POST endpoint: Scrape a single URL
# # # -----------------------------
# # @router.post("/", response_model=ScrapedPageResponse)
# # async def scrape_page(payload: UrlRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
# #     url = normalize_url(payload.url)
# #     try:
# #         text_content = await get_rendered_page(url)
# #         text_content = clean_repetitions(text_content)
# #     except Exception as e:
# #         raise HTTPException(status_code=400, detail=f"Error fetching URL: {str(e)}")

# #     # Hierarchical / semantic chunking
# #     chunks = chunk_text([{"url": url, "content": text_content}])
# #     existing_page = db.query(ScrapedPage).filter(ScrapedPage.url == url).first()

# #     if existing_page:
# #         existing_page.content = text_content
# #         existing_page.last_synced = datetime.utcnow()
# #         db.commit()
# #         db.refresh(existing_page)
# #         background_tasks.add_task(generate_and_store_embeddings, existing_page.id, chunks, db)
# #         return ScrapedPageResponse(url=existing_page.url, content=existing_page.content)

# #     new_page = ScrapedPage(
# #         url=url,
# #         content=text_content,
# #         last_synced=datetime.utcnow()
# #     )
# #     db.add(new_page)
# #     db.commit()
# #     db.refresh(new_page)
# #     background_tasks.add_task(generate_and_store_embeddings, new_page.id, chunks, db)
# #     return ScrapedPageResponse(url=new_page.url, content=new_page.content)

# # # -----------------------------
# # # GET endpoint: Retrieve scraped page by URL
# # # -----------------------------
# # @router.get("/by_url", response_model=ScrapedPageResponse)
# # def get_scraped_page(url: str, db: Session = Depends(get_db)):
# #     content = fetch_scraped_from_redis(url)
# #     if content:
# #         return ScrapedPageResponse(url=normalize_url(url), content=content)

# #     page = db.query(ScrapedPage).filter(ScrapedPage.url == normalize_url(url)).first()
# #     if not page:
# #         raise HTTPException(status_code=404, detail="Page not found")
# #     return ScrapedPageResponse(url=page.url, content=page.content)

# # # -----------------------------
# # # POST endpoint: Scrape multiple URLs
# # # -----------------------------
# # @router.post("/batch")
# # async def scrape_multiple(req: UrlListRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
# #     results = []
# #     async with httpx.AsyncClient() as client:
# #         for raw_url in req.urls:
# #             url = normalize_url(raw_url)
# #             try:
# #                 text_content = await get_rendered_page(url)
# #                 text_content = clean_repetitions(text_content)
# #                 chunks = chunk_text([{"url": url, "content": text_content}])
# #                 existing_page = db.query(ScrapedPage).filter(ScrapedPage.url == url).first()

# #                 if existing_page:
# #                     existing_page.content = text_content
# #                     existing_page.last_synced = datetime.utcnow()
# #                     db.commit()
# #                     db.refresh(existing_page)
# #                     background_tasks.add_task(generate_and_store_embeddings, existing_page.id, chunks, db)
# #                     results.append({"url": existing_page.url, "content": existing_page.content})
# #                 else:
# #                     new_page = ScrapedPage(
# #                         url=url,
# #                         content=text_content,
# #                         last_synced=datetime.utcnow()
# #                     )
# #                     db.add(new_page)
# #                     db.commit()
# #                     db.refresh(new_page)
# #                     background_tasks.add_task(generate_and_store_embeddings, new_page.id, chunks, db)
# #                     results.append({"url": new_page.url, "content": new_page.content})

# #             except Exception as e:
# #                 results.append({"url": url, "error": str(e)})

# #     return {"results": results}



from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.model.scraped_page import ScrapedPage
# i changed this
from app.utils.text_chunker import chunk_text
from app.generic.embedding import _MODEL  # access model for batch encode
from app.utils.rendered_scraper import get_rendered_page

# Redis helpers
#from app.generic.scraped_gen import redis_client, SCRAPED_KEY, load_scraped_to_redis, fetch_scraped_from_redis
from app.generic.scraped_gen import update_single_page_in_redis, fetch_scraped_from_redis


import httpx
import json
import asyncio
import numpy as np

# -----------------------------
# Pydantic schemas
# -----------------------------
class UrlRequest(BaseModel):
    url: str

class UrlListRequest(BaseModel):
    urls: list[str]

class ScrapedPageResponse(BaseModel):
    url: str
    content: str

# -----------------------------
# FastAPI router
# -----------------------------
router = APIRouter(
    prefix="/api/scrape",
    tags=["scrape"]
)

# -----------------------------
# Utility: Normalize URL
# -----------------------------
def normalize_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return "https://" + url
    return url

# -----------------------------
# Background task: Generate embeddings and refresh Redis once
# -----------------------------
def generate_and_store_embeddings(page_id: int, chunks: list, db: Session):
    embeddings = _MODEL.encode(chunks, show_progress_bar=False, convert_to_numpy=True).astype(float).tolist()
    page = db.query(ScrapedPage).filter(ScrapedPage.id == page_id).first()
    if page:
        page.chunks = json.dumps(chunks)
        page.embedding = json.dumps(embeddings)
        db.commit()

        # Refresh only new data to Redis cache only once here
        try:
            asyncio.run(update_single_page_in_redis(page_id))
        except Exception as e:
            print(f"⚠️ Failed to update Redis for page {page_id}: {e}")


# -----------------------------
# POST endpoint: Scrape a single URL
# -----------------------------
@router.post("/", response_model=ScrapedPageResponse)
async def scrape_page(payload: UrlRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    url = normalize_url(payload.url)
    try:
        text_content = await get_rendered_page(url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error fetching URL: {str(e)}")

    chunks = chunk_text(text_content)
    existing_page = db.query(ScrapedPage).filter(ScrapedPage.url == url).first()

    if existing_page:
        existing_page.content = text_content
        db.commit()
        db.refresh(existing_page)
        background_tasks.add_task(generate_and_store_embeddings, existing_page.id, chunks, db)
        return ScrapedPageResponse(url=existing_page.url, content=existing_page.content)

    new_page = ScrapedPage(url=url, content=text_content)
    db.add(new_page)
    db.commit()
    db.refresh(new_page)
    background_tasks.add_task(generate_and_store_embeddings, new_page.id, chunks, db)

    return ScrapedPageResponse(url=new_page.url, content=new_page.content)

# -----------------------------
# GET endpoint: Retrieve scraped page by URL
# -----------------------------
@router.get("/by_url", response_model=ScrapedPageResponse)
def get_scraped_page(url: str, db: Session = Depends(get_db)):
    """Retrieve scraped page by URL: first try Redis, fallback to Postgres"""
    content = fetch_scraped_from_redis(url)
    if content:
        return ScrapedPageResponse(url=normalize_url(url), content=content)

    # Fallback: Postgres
    page = db.query(ScrapedPage).filter(ScrapedPage.url == normalize_url(url)).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    return ScrapedPageResponse(url=page.url, content=page.content)

# -----------------------------
# POST endpoint: Scrape multiple URLs
# -----------------------------
@router.post("/batch")
async def scrape_multiple(req: UrlListRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    results = []
    async with httpx.AsyncClient() as client:
        for raw_url in req.urls:
            url = normalize_url(raw_url)
            try:
                text_content = await get_rendered_page(url)
                chunks = chunk_text(text_content)
                existing_page = db.query(ScrapedPage).filter(ScrapedPage.url == url).first()

                if existing_page:
                    existing_page.content = text_content
                    db.commit()
                    db.refresh(existing_page)
                    background_tasks.add_task(generate_and_store_embeddings, existing_page.id, chunks, db)
                    results.append({"url": existing_page.url, "content": existing_page.content})
                else:
                    new_page = ScrapedPage(url=url, content=text_content)
                    db.add(new_page)
                    db.commit()
                    db.refresh(new_page)
                    background_tasks.add_task(generate_and_store_embeddings, new_page.id, chunks, db)
                    results.append({"url": new_page.url, "content": new_page.content})

            except Exception as e:
                results.append({"url": url, "error": str(e)})

    # (Removed redundant Redis refresh)
    return {"results": results}