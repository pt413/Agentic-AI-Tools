# from redis import asyncio as aioredis
# import json
# import os
# from app.model.faq_model import FAQ  # your ORM model for faq table
# from app.db.database import SessionLocal
# import asyncio

# REDIS_HOST = os.getenv("REDIS_HOST", "redis")
# REDIS_PORT = os.getenv("REDIS_PORT", "6379")

# REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
# REDIS_URL = os.getenv("REDIS")
# # Corrected line - use from_url instead of fr
# redis_client = aioredis.from_url(
#     REDIS_URL, 
#     encoding="utf-8", 
#     decode_responses=True
# )

# FAQ_KEY = "faq_global"

# async def load_faq_to_redis():
#     with SessionLocal() as db:
#         faqs = db.query(FAQ).all()
#         faq_list = []
#         for f in faqs:
#             faq_list.append({
#                 "id": f.id,
#                 "question": f.question,
#                 "answer": f.answer,
#                 "faq_vector": f.faq_vector.tolist()  # Convert ndarray -> list
#             })
#         await redis_client.set(FAQ_KEY, json.dumps(faq_list))  # no TTL

# async def search_faq(query_vec, top_k=5):
#     cached_faqs = await redis_client.get(FAQ_KEY)
#     if not cached_faqs:
#         return []

#     faqs = json.loads(cached_faqs)
    
#     # Simple cosine similarity or dot product with query_vec
#     def score(faq_vec):
#         return sum([a*b for a,b in zip(faq_vec, query_vec)])

#     for f in faqs:
#         f['score'] = score(f['faq_vector'])

#     faqs.sort(key=lambda x: x['score'], reverse=True)
#     return faqs[:top_k]


# async def check_faq():
#     cached_faqs = await redis_client.get("faq_global")
#     if cached_faqs:
#         faqs = json.loads(cached_faqs)
#         print(f"Total FAQs in Redis: {len(faqs)}")
#         for f in faqs[:5]:
#             print(f"Q: {f['question']}\nA: {f['answer']}\nA: {f['faq_vector']}\n")
#     else:
#         print("No FAQs found in Redis!")
