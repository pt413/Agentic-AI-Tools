#from redis import asyncio as aioredis
import redis.asyncio as aioredis
import json
import os
from app.model.scraped_page import ScrapedPage
from app.db.database import SessionLocal
import asyncio
#import redis.asyncio as redis           #to use redis on the place of aioredis


# -----------------------------------
# Redis setup
# -----------------------------------
#REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_HOST = "127.0.0.1"
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"

redis_client = aioredis.from_url(
    REDIS_URL,
    encoding="utf-8",
    decode_responses=True
)

SCRAPED_KEY = "scraped_global"


# -----------------------------------
# 1️⃣ Load scraped data from PostgreSQL → Redis
# -----------------------------------
async def load_scraped_to_redis():
    """
    Load all scraped pages (with embeddings) from Postgres into Redis.
    Each chunk is stored inside a single JSON list under 'scraped_global'.
    """
    with SessionLocal() as db:
        pages = db.query(ScrapedPage).all()
        all_chunks = []

        for page in pages:
            chunks = json.loads(page.chunks) if page.chunks else []
            embeddings = json.loads(page.embedding) if page.embedding else []

            for i, chunk in enumerate(chunks):
                emb = embeddings[i] if i < len(embeddings) else [0.0] * 384
                all_chunks.append({
                    "id": f"{page.id}_{i}",
                    "url": page.url,
                    "chunk": chunk,
                    "embedding": emb
                })

        await redis_client.set(SCRAPED_KEY, json.dumps(all_chunks))
        print(f"✅ Loaded {len(all_chunks)} chunks from PostgreSQL into Redis under key '{SCRAPED_KEY}'.")


# -----------------------------------
# 2️⃣ Search scraped data using embedding similarity
# -----------------------------------
async def search_scraped(query_vec, top_k=5):
    """
    Compute top-K most similar scraped chunks based on dot product similarity.
    """
    cached = await redis_client.get(SCRAPED_KEY)
    if not cached:
        print("⚠️ No scraped data found in Redis.")
        return []

    pages = json.loads(cached)

    def score(vec):
        return sum(a * b for a, b in zip(vec, query_vec))

    for p in pages:
        p["score"] = score(p["embedding"])

    pages.sort(key=lambda x: x["score"], reverse=True)
    return pages[:top_k]


# -----------------------------------
# 3️⃣ Inspect what's inside Redis
# -----------------------------------
async def check_scraped():
    cached = await redis_client.get(SCRAPED_KEY)
    if cached:
        pages = json.loads(cached)
        print(f"📦 Total Scraped Chunks in Redis: {len(pages)}")
        for p in pages[:3]:
            print(f"🔹 URL: {p['url']}")
            print(f"Chunk (first 100 chars): {p['chunk'][:100]}...")
            print(f"Embedding len: {len(p['embedding'])}")
            print()
    else:
        print("No scraped data found in Redis!")


# -----------------------------------
# 4️⃣ Fetch scraped data from Redis
# -----------------------------------
async def fetch_scraped_from_redis(url: str = None, page_id: int = None):
    """
    Fetch all chunks for a specific URL or page ID from Redis.
    Returns a list of {id, url, chunk, embedding}.
    """
    client = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)

    cached = await client.get(SCRAPED_KEY)
    await client.aclose()

    if not cached:
        print("⚠️ No scraped data cached in Redis.")
        return []

    try:
        pages = json.loads(cached)
        print(f"✅ Loaded {len(pages)} pages from Redis.")
        if pages:
            print("Sample keys:", pages[0].keys())
    except Exception as e:
        print("❌ JSON decode error:", e)
        return []

    # Optional filtering
    if url:
        filtered = [p for p in pages if p.get("url") == url]
        print(f"🔍 Found {len(filtered)} pages for URL filter.")
        return filtered

    if page_id is not None:
        filtered = [p for p in pages if str(p.get("id", "")).startswith(f"{page_id}_")]
        print(f"🔍 Found {len(filtered)} pages for page_id filter.")
        return filtered

    return pages  # if no filter provided, return all

# -----------------------------------
# 1️⃣ Load only new scraped data from PostgreSQL → Redis
# -----------------------------------
async def update_single_page_in_redis(page_id: int):
    """
    Update only one page (newly scraped or updated) into Redis
    without reloading all data.
    """
    client = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    db = SessionLocal()

    try:
        page = db.query(ScrapedPage).filter(ScrapedPage.id == page_id).first()
        if not page:
            print(f"⚠️ No page found with ID {page_id}")
            return

        # Load the existing cached data first
        cached = await client.get(SCRAPED_KEY)
        pages = json.loads(cached) if cached else []

        # Remove old version of this page (if exists)
        pages = [p for p in pages if p.get("id") != page.id]

        # Add updated/new page
        new_entry = {
            "id": page.id,
            "url": page.url,
            "content": page.content,
            "chunks": json.loads(page.chunks) if page.chunks else [],
            "embedding": json.loads(page.embedding) if page.embedding else []
        }
        pages.append(new_entry)

        # Write updated data back
        await client.set(SCRAPED_KEY, json.dumps(pages))
        print(f"✅ Updated page {page.url} in Redis")
    except Exception as e:
        print(f"❌ Error updating Redis for page {page_id}: {e}")
    finally:
        await client.aclose()
        db.close()
