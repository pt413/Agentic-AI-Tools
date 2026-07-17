# import redis.asyncio as redis
# import os
# from datetime import datetime, timedelta
# import uuid
# import hashlib
# import json
# import asyncio
# from fastapi import APIRouter, HTTPException, Depends
# from sqlalchemy.orm import Session
# from concurrent.futures import ThreadPoolExecutor
# from app.db.database import SessionLocal
# # from app.model.user import User
# from app.model.chat_history import UserChatMessage
# from app.llm_mcp.llm_call import llm_func_call
# from app.llm_mcp.sessionManagement import SessionManager, SmartCacheManager, RateLimiter
# from app.generic.query_relation import check_query_relation
# from app.utils.intent_engine import detect_query_intent
# from app.generic.context_manager import build_session_context
# import time



# import redis
# import os
# REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
# REDIS_PORT = os.getenv("REDIS_PORT", "6379")
# REDIS = f"redis://{REDIS_HOST}:{REDIS_PORT}"
# REDIS = os.getenv("REDIS")
# db_executor = ThreadPoolExecutor(max_workers=5)

# import redis
# REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
# REDIS_PORT = os.getenv("REDIS_PORT", "6379")
# REDIS = f"redis://{REDIS_HOST}:{REDIS_PORT}"
# # REDIS = os.getenv("REDIS")
# db_executor = ThreadPoolExecutor(max_workers=5)


# def get_db():
#     db = SessionLocal()
#     try:
#         yield db
#     finally:
#         db.close()

# redis_client = redis.from_url(REDIS, encoding="utf-8", decode_responses=True)

# class BackgroundDBManager:
#     def __init__(self):
#         pass
    
#     async def store_message_background(self, user_id: int, session_id: str, question: str, query_data: dict, answer: str):
#         """Store message in background thread with NEW database session"""
#         try:
#             # Run in thread pool to avoid blocking
#             loop = asyncio.get_event_loop()
#             await loop.run_in_executor(
#                 db_executor,
#                 self._sync_store_message, 
#                 user_id, session_id, question, query_data ,answer
#             )
#             print(f" Message stored in background for user {user_id}")
#         except Exception as e:
#             print(f" Background DB storage failed: {e}")
    
#     def _sync_store_message(self, user_id: int, session_id: str, question: str, query_data: dict, answer: str):
#         """Synchronous method with NEW database session"""
#         # Create a NEW database session for the background thread
#         db = SessionLocal()
#         try:
#             new_message = UserChatMessage(
#                 user_id=user_id,
#                 chat_session_id=session_id,
#                 question=question,
#                 ans=json.dumps(answer) if isinstance(answer, (dict, list)) else answer,
#                 query_data=json.dumps(query_data, default=str),

#                 created_at=datetime.now()
#             )
            
#             db.add(new_message)
#             db.commit()
#             db.refresh(new_message)
#             print(f"📝 Background DB: Stored message ID {new_message.id} for user {user_id}")
            
#         except Exception as e:
#             db.rollback()
#             print(f"❌ Background DB Error: {e}")
#         finally:
#             # Always close the session in the background thread
#             db.close()

# # Initialize managers
# session_manager = SessionManager(redis_client)
# cache_manager = SmartCacheManager(redis_client)
# rate_limiter = RateLimiter(redis_client)
# background_db = BackgroundDBManager()
# router = APIRouter(prefix="/api/bp_ai", tags=["BP_AI"])
# app/routes/bp_ai_route.py
from __future__ import annotations
from typing import Optional, Any, Dict, Callable, Awaitable
import logging
import json
import asyncio

from fastapi import APIRouter, Depends, HTTPException, status, Query, Body, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db

from app.db.database import SessionLocal
from app.dependencies import get_chat_service, get_current_user
class WebsocketSocketAdapter:
    """
    Minimal adapter that wraps a FastAPI/WebSocket and exposes a small
    publisher API used by the orchestrator/chat_service:
      - publish_chunk(chunk)
      - publish_done(final, sources=None, followups=None)
      - publish_error(msg)
    """

    def __init__(self, websocket: WebSocket, session_id: Optional[str] = None):
        self._ws = websocket
        self.session_id = session_id

    async def publish_chunk(self, chunk: str, meta: Optional[dict] = None) -> None:
        try:
            # DEBUG: log each chunk we attempt to send
            logger.debug("publish_chunk: session=%s chunk_len=%d meta_keys=%s",
                         self.session_id, (len(chunk) if chunk is not None else 0),
                         list((meta or {}).keys()))
            await self._ws.send_json({"type": "chunk", "chunk": chunk, "meta": meta or {}})
        except Exception:
            # keep existing behavior but include exception details (already uses logger.exception)
            logger.exception("publish_chunk: failed to send chunk over WS (session=%s)", self.session_id)

    async def publish_done(self, final: str, sources: Optional[list] = None, followups: Optional[list] = None) -> None:
        try:
            payload = {"type": "done", "final": final}
            if sources is not None:
                payload["sources"] = sources
            if followups is not None:
                payload["followups"] = followups

            # DEBUG: log final payload summary (avoid logging huge final text in full)
            try:
                final_preview = (final[:200] + "...") if final and len(final) > 200 else final
            except Exception:
                final_preview = "<unable to preview>"
            logger.debug("publish_done: session=%s final_preview=%s sources=%s followups=%s",
                         self.session_id, final_preview,
                         (len(sources) if sources is not None else 0),
                         (len(followups) if followups is not None else 0))

            await self._ws.send_json(payload)
        except Exception:
            logger.exception("publish_done: failed to send final over WS (session=%s)", self.session_id)

    async def publish_error(self, message: str) -> None:
        try:
            await self._ws.send_json({"type": "error", "message": message})
        except Exception:
            logger.exception("publish_error: failed to send error over WS")
logger = logging.getLogger("app.routes.bp_ai")

router = APIRouter(prefix="/api/bp_ai", tags=["BP_AI"])

# async def get_user_context(user_id: int, db: Session):
#     """Fetch user profile + last 24h chat history"""
#     user = db.query(User).filter(User.id == user_id).first()
#     if not user:
#         raise HTTPException(status_code=404, detail="User not found")

#     user_context = f"""
#     User Profile:
#     Name: {user.first_name} {user.last_name}
#     Designation: {user.designation}
#     Department: {user.department}
#     Education: {user.education}
#     Email: {user.email}
#     """
#     return user_context


# def create_stable_cache_key(user_id: int, query: str, system_prompt: str, bookingid: str = None, leadid: str = None, previous_query: str = "") -> str:
#     """Create a stable cache key that doesn't change between requests"""
#     normalized_query = query.strip().lower()
#     normalized_system_prompt = system_prompt.strip().lower()
#     normalized_bookingid = (bookingid or "").strip().lower()
#     normalized_leadid = (leadid or "").strip().lower()
#     normalized_previous = previous_query.strip().lower()
    
#     key_string = f"{user_id}:{normalized_query}:{normalized_system_prompt}:{normalized_bookingid}:{normalized_leadid}:{normalized_previous}"
    
#     hash_object = hashlib.sha256(key_string.encode('utf-8'))
#     hex_dig = hash_object.hexdigest()
    
#     return f"cache:user:{user_id}:response:{hex_dig}"
# from app.generic.generic_utils import get_season_data
# @router.get("/vector_search")
# async def vector_search(
#     query: str, 
#     load:str,
#     user_id: int, 
#     tts_enabled: bool = True,
#     session_id: str = None, 
#     bookingid: str = None,
#     leadId: str = None,
#     system_prompt: str = "support",
#     previous_query: str = "",
#     db: Session = Depends(get_db)
# ):
#     request_start_time = time.time()
#     print(f"🧭 previous: {previous_query}")

#     try:
#         pre_data = None
#         if session_id:
#             pre_data= await get_season_data(session_id)

#         #Get user context
#         # user_context = await get_user_context(user_id, db)
        
#         # 2️⃣ Extract query intent
#         # intent = await detect_query_intent(query, user_context, db)
#         # print(f"🧭 Detected intent: {intent}")

       

#         # last_message = (
#         #     db.query(UserChatMessage)
#         #     .filter(UserChatMessage.user_id == user_id)
#         #     .order_by(UserChatMessage.created_at.desc())
#         #     .first()
#         # )

#         # previous_query = last_message.question if last_message else ""
#         # previous_answer = last_message.ans if last_message else ""

#         # relation_info = await check_query_relation(previous_query, query)
#         # print(f"🔗 Query relation: {relation_info}")

#         # 4️⃣ Build the actual query to send to LLM
#         # if session_id:
#         #     context_query = await build_session_context(db, session_id, query)
#         # else:

#         context_query = f"if if incompleted question then follow the previous  question and answer \nprevious followup query: {previous_query}\n just previous answer: {pre_data}\ncurrent query: {query}"

#         context_query += f"\nAdditional Load: {load}"
#         if bookingid or leadId:
#             context_query += f"\nAvailable IDs - Booking: {bookingid}, Lead: {leadId}"
            
#         print(f" Context query: {context_query}")
#         tts_enabled = str(tts_enabled).lower() in ("true", "1")

#         llm_start = time.time()
#         final_response = await llm_func_call(
#             query=context_query, 
#             system_prompt=system_prompt,
#             booking_id=bookingid,
#             lead_id=leadId
#         )

#         llm_total_time = time.time() - llm_start
#         print(f"  LLM Processing Total: {llm_total_time:.2f}s")
        
#         if not final_response:
#             return {"status": "error", "response": "No response from LLM"}

#         if not session_id:
#             session_id = str(uuid.uuid4())
        
#         answer_text = final_response.get('final_response', '')
#         audio_bytes = None
#         # if tts_enabled:
#         #     from app.utils.tts import generate_tts
#         #     audio_bytes = await generate_tts(answer_text)
#         print("user detaisl ", user_id, session_id, query, final_response)
#         with SessionLocal() as db:
#             asyncio.create_task(
#                 background_db.store_message_background(
#                     user_id=user_id,
#                     session_id=session_id,
#                     question=query,
#                     query_data=final_response.get('function_result', {}),
#                     answer=final_response.get('final_response', '')
#                 )
#             )
#         print("Database storage started in background")

#         result_data = {
#             "status": "success",
#             "intent": "",
#             "response": final_response,
#             "chat_session_id": session_id,
#             # "audio_bytes": audio_bytes.decode("latin1") if audio_bytes else None,
#             audio_bytes:None,
#             "usage_count": await rate_limiter.get_usage_stats(user_id),
#             "performance_metrics": {
#                 "total_time": round(time.time() - request_start_time, 2),
#                 "llm_time": round(llm_total_time, 2),
#                 # "cache_time": round(cache_time, 2),
#                 "background_db": True  
#             }
#         }

#         # cache_store_start = time.time()
#         # await cache_manager.set_cached_response(cache_key, result_data)
#         # cache_store_time = time.time() - cache_store_start
#         # print(f"⏱  Cache storage: {cache_store_time:.2f}s")
        
#         # total_request_time = time.time() - request_start_time
#         # print(f"=> TOTAL REQUEST TIME: {total_request_time:.2f}s")
        
#         return result_data

#     except Exception as e:
#         error_time = time.time() - request_start_time
#         print(f" REQUEST FAILED after {error_time:.2f}s: {e}")
#         return {"status": "error", "response": "Service temporarily unavailable. Please try again later."}

# @router.get("/user_history")
# async def get_user_history(user_id: int, db: Session = Depends(get_db)):
#     """Get user's chat history"""
#     messages = (
#         db.query(UserChatMessage)
#         .filter(UserChatMessage.user_id == user_id)
#         .order_by(UserChatMessage.created_at.asc())
#         .all()
#     )

#     if not messages:
#         return {"status": "error", "response": "no chat history found"}

#     seen = set()
#     all_questions = []
#     for m in messages:
#         if m.question not in seen:
#             all_questions.append(m.question)
#             seen.add(m.question)
#     history_list = [
#         {
#             "id": m.id,
#             "chat_session_id": m.chat_session_id,
#             "question": m.question,
#             "ans": m.ans,
#             "query_data": m.query_data,
#             "created_at": m.created_at,
#         }
#         for m in messages
#     ]

#     return {
#         "status": "success",
#         "response":  history_list
#     }

# @router.on_event("startup")
# async def startup_cleanup():
#     """Start background cleanup tasks"""
#     asyncio.create_task(periodic_cleanup())

# async def periodic_cleanup():
#     """Periodic cleanup of old cache and sessions"""
#     while True:
#         try:
#             await cache_manager.cleanup_old_cache()
#             print(" Periodic cleanup completed")
#         except Exception as e:
#             print(f"Cleanup error: {e}")
        
#         # Run every hour
#         await asyncio.sleep(3600)

# @router.on_event("shutdown")
# async def shutdown_event():
#     """Cleanup background workers on shutdown"""
#     print(" Shutting down background workers...")
#     db_executor.shutdown(wait=True)
#     print(" Background workers shut down")

# from app.generic.llm_ans import gemini
# @router.get("/auto_suggestion")
# async def autosuggetion(question:str=None):
#     """Autocomplete suggestions for the chatbot"""
#     if not question:
#         return {"status": "error", "response": "None"}
#     cust_prompt = f"""
#         Generate exactly 5 related questions based on the following user query as admin type question and also make sure those variable values null don't consider in question generation.:

#         question:"{question}"

#         Return only the 5 questions, separated by , with no extra text like example or response ["question1", "question2",...etc].
#         """

#     response =await gemini(question, cust_prompt)
#     print(f"Auto suggestions: {response}")
#     if response:
#         return {"status": "success", "response": response}
#     return {"status": "error", "response": "No suggestions found"}

# from app.generic.external_data import scrap_data
# @router.get("/scrap")
# async def get_scrap():
#     """Scrap data from a website"""
#     # Replace with actual scraping logic
#     data = await scrap_data()
#     print(f"Scraped data: {data}")
#     if data is None:
#         return {"status": "error", "response": "No data found"}
    
#     return {"status": "success", "response": data}
#     # Example:
# # from app.generic.external_data import faq_search
# # from sqlalchemy.ext.asyncio import AsyncSession
# # @router.get("/faq/search")
# # async def search_faq(query: str):
# #     return await faq_search(query)
