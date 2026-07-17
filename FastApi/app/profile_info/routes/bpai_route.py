import redis.asyncio as redis
import os
from datetime import datetime, timedelta
import uuid
import hashlib
import json
import asyncio
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from concurrent.futures import ThreadPoolExecutor
from app.db.database import SessionLocal
from app.BrightpathAI.models.users import User
from app.BrightpathAI.models.chat_history import UserChatMessage
from app.BrightpathAI.services.mcp_server import llm_func_call
from app.llm_mcp.sessionManagement import SessionManager, SmartCacheManager, RateLimiter
# from app.generic.query_relation import check_query_relation
# from app.utils.intent_engine import detect_query_intent
# from app.generic.context_manager import build_session_context
import time

import redis
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS = f"redis://{REDIS_HOST}:{REDIS_PORT}"
# REDIS = os.getenv("REDIS")
db_executor = ThreadPoolExecutor(max_workers=5)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

redis_client = redis.from_url(REDIS, encoding="utf-8", decode_responses=True)

class BackgroundDBManager:
    def __init__(self):
        pass
    
    async def store_message_background(self, user_id: int, session_id: str, question: str, query_data: dict, answer: str):
        """Store message in background thread with NEW database session"""
        try:
            # Run in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                db_executor,
                self._sync_store_message, 
                user_id, session_id, question, query_data ,answer
            )
            print(f" Message stored in background for user {user_id}")
        except Exception as e:
            print(f" Background DB storage failed: {e}")
    
    def _sync_store_message(self, user_id: int, session_id: str, question: str, query_data: dict, answer: str):
        """Synchronous method with NEW database session"""
        # Create a NEW database session for the background thread
        db = SessionLocal()
        try:
            new_message = UserChatMessage(
                user_id=user_id,
                chat_session_id=session_id,
                question=question,
                ans=json.dumps(answer) if isinstance(answer, (dict, list)) else answer,
                query_data=json.dumps(query_data, default=str),

                created_at=datetime.now()
            )
            
            db.add(new_message)
            db.commit()
            db.refresh(new_message)
            print(f"📝 Background DB: Stored message ID {new_message.id} for user {user_id}")
            
        except Exception as e:
            db.rollback()
            print(f"❌ Background DB Error: {e}")
        finally:
            # Always close the session in the background thread
            db.close()

# Initialize managers
session_manager = SessionManager(redis_client)
cache_manager = SmartCacheManager(redis_client)
rate_limiter = RateLimiter(redis_client)
background_db = BackgroundDBManager()
router = APIRouter(prefix="/api/bp_ai", tags=["BP_AI"])


async def get_user_context(user_id: int, db: Session):
    """Fetch user profile + last 24h chat history"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user_context = f"""
    User Profile:
    Name: {user.first_name} {user.last_name}
    Designation: {user.designation}
    Department: {user.department}
    Education: {user.education}
    Email: {user.email}
    """
    return user_context


def create_stable_cache_key(user_id: int, query: str, system_prompt: str, bookingid: str = None, leadid: str = None, previous_query: str = "") -> str:
    """Create a stable cache key that doesn't change between requests"""
    normalized_query = query.strip().lower()
    normalized_system_prompt = system_prompt.strip().lower()
    normalized_bookingid = (bookingid or "").strip().lower()
    normalized_leadid = (leadid or "").strip().lower()
    normalized_previous = previous_query.strip().lower()
    
    key_string = f"{user_id}:{normalized_query}:{normalized_system_prompt}:{normalized_bookingid}:{normalized_leadid}:{normalized_previous}"
    
    hash_object = hashlib.sha256(key_string.encode('utf-8'))
    hex_dig = hash_object.hexdigest()
    
    return f"cache:user:{user_id}:response:{hex_dig}"
from app.BrightpathAI.utils.generic_utils import get_season_data
from app.BrightpathAI.services.live_streaming import broadcaster as EventBroadcaster
@router.get("/vector_search")
async def vector_search(
    query: str, 
    load:str,
    user_id: int, 
    tts_enabled: bool = True,
    session_id: str = None, 
    bookingid: str = None,
    leadId: str = None,
    system_prompt: str = "support",
    previous_query: str = "",
    db: Session = Depends(get_db)
):
    request_start_time = time.time()
    print(f"🧭 previous: {previous_query}")
    await EventBroadcaster.publish("🔍 Analyzing your question...")

    try:
        pre_data = None
        if session_id:
            await EventBroadcaster.publish("📚 Loading conversation context...")
            pre_data= await get_season_data(session_id)


        #Get user context
        # user_context = await get_user_context(user_id, db)
        
        # 2️⃣ Extract query intent
        # intent = await detect_query_intent(query, user_context, db)
        # print(f"🧭 Detected intent: {intent}")

       

        # last_message = (
        #     db.query(UserChatMessage)
        #     .filter(UserChatMessage.user_id == user_id)
        #     .order_by(UserChatMessage.created_at.desc())
        #     .first()
        # )

        # previous_query = last_message.question if last_message else ""
        # previous_answer = last_message.ans if last_message else ""

        # relation_info = await check_query_relation(previous_query, query)
        # print(f"🔗 Query relation: {relation_info}")

        # 4️⃣ Build the actual query to send to LLM
        # if session_id:
        #     context_query = await build_session_context(db, session_id, query)
        # else:
        await EventBroadcaster.publish("🛠️ Building search query...")
        context_query = f"if if incompleted question then follow the previous  question and answer \nprevious followup query: {previous_query}\n just previous answer: {pre_data}\ncurrent query: {query}"

        context_query += f"\nAdditional Load: {load}"
        if bookingid or leadId:
            await EventBroadcaster.publish("🎯Identifying key")
            context_query += f"\nAvailable IDs - Booking: {bookingid}, Lead: {leadId}"

            
        print(f" Context query: {context_query}")
        tts_enabled = str(tts_enabled).lower() in ("true", "1")
        await EventBroadcaster.publish("🚀 Processing AI Engine")
        llm_start = time.time()
        final_response = await llm_func_call(
            query=context_query, 
            system_prompt=system_prompt,
            booking_id=bookingid,
            lead_id=leadId
        )

        llm_total_time = time.time() - llm_start
        print(f"  LLM Processing Total: {llm_total_time:.2f}s")
        
        if not final_response:
            return {"status": "error", "response": "No response from LLM"}

        if not session_id:
            session_id = str(uuid.uuid4())
        
        answer_text = final_response.get('final_response', '')
        audio_bytes = None
        # if tts_enabled:
        #     from app.utils.tts import generate_tts
        #     audio_bytes = await generate_tts(answer_text)
        print("user detaisl ", user_id, session_id, query, final_response)
        with SessionLocal() as db:
            asyncio.create_task(
                background_db.store_message_background(
                    user_id=user_id,
                    session_id=session_id,
                    question=query,
                    query_data=final_response.get('function_result', {}),
                    answer=final_response.get('final_response', '')
                )
            )
        print("Database storage started in background")

        result_data = {
            "status": "success",
            "intent": "",
            "response": final_response,
            "chat_session_id": session_id,
            # "audio_bytes": audio_bytes.decode("latin1") if audio_bytes else None,
            audio_bytes:None,
            "usage_count": await rate_limiter.get_usage_stats(user_id),
            "performance_metrics": {
                "total_time": round(time.time() - request_start_time, 2),
                "llm_time": round(llm_total_time, 2),
                # "cache_time": round(cache_time, 2),
                "background_db": True  
            }
        }

        # cache_store_start = time.time()
        # await cache_manager.set_cached_response(cache_key, result_data)
        # cache_store_time = time.time() - cache_store_start
        # print(f"⏱  Cache storage: {cache_store_time:.2f}s")
        
        # total_request_time = time.time() - request_start_time
        # print(f"=> TOTAL REQUEST TIME: {total_request_time:.2f}s")
        
        return result_data

    except Exception as e:
        error_time = time.time() - request_start_time
        print(f" REQUEST FAILED after {error_time:.2f}s: {e}")
        return {"status": "error", "response": "Service temporarily unavailable. Please try again later."}

@router.get("/user_history")
async def get_user_history(user_id: int, db: Session = Depends(get_db)):
    """Get user's chat history"""
    messages = (
        db.query(UserChatMessage)
        .filter(UserChatMessage.user_id == user_id)
        .order_by(UserChatMessage.created_at.asc())
        .limit(20)
    )

    if not messages:
        return {"status": "error", "response": "no chat history found"}

    seen = set()
    all_questions = []
    for m in messages:
        if m.question not in seen:
            all_questions.append(m.question)
            seen.add(m.question)
    history_list = [
        {
            "id": m.id,
            "chat_session_id": m.chat_session_id,
            "question": m.question,
            "ans": m.ans,
            "query_data": m.query_data,
            "created_at": m.created_at,
        }
        for m in messages
    ]

    return {
        "status": "success",
        "response":  history_list
    }

@router.on_event("startup")
async def startup_cleanup():
    """Start background cleanup tasks"""
    asyncio.create_task(periodic_cleanup())

async def periodic_cleanup():
    """Periodic cleanup of old cache and sessions"""
    while True:
        try:
            await cache_manager.cleanup_old_cache()
            print(" Periodic cleanup completed")
        except Exception as e:
            print(f"Cleanup error: {e}")
        
        # Run every hour
        await asyncio.sleep(3600)

@router.on_event("shutdown")
async def shutdown_event():
    """Cleanup background workers on shutdown"""
    print(" Shutting down background workers...")
    db_executor.shutdown(wait=True)
    print(" Background workers shut down")

from app.generic.llm_ans import gemini
@router.get("/auto_suggestion")
async def autosuggetion(question:str=None):
    """Autocomplete suggestions for the chatbot"""
    if not question:
        return {"status": "error", "response": "None"}
    cust_prompt = f"""
        Generate exactly 5 related questions based on the following user query as admin type question and also make sure those variable values null don't consider in question generation.:

        question:"{question}"

        Return only the 5 questions, separated by , with no extra text like example or response ["question1", "question2",...etc].
        """

    response =await gemini(question, cust_prompt)
    print(f"Auto suggestions: {response}")
    if response:
        return {"status": "success", "response": response}
    return {"status": "error", "response": "No suggestions found"}

from fastapi import APIRouter, Depends
from sqlalchemy import func, and_
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
import asyncio, time

from app.db.database import SessionLocal
from app.model.lead_details import LeadActivity
from app.model.lead_performance import LeadPerformanceMetrics, ExecutivePerformanceAggregates, PerformanceSuggestions

# router = APIRouter()

# Helper to safely run query in its own session
def run_in_new_session(func):
    def wrapper(*args, **kwargs):
        with SessionLocal() as db:
            return func(db, *args, **kwargs)
    return wrapper


@router.get("/overview")
async def get_dashboard_overview():
    """Optimized async dashboard overview (same format)."""
    start_time = time.time()
    now = datetime.now(timezone.utc)
    last_24h = now - timedelta(hours=24)

    try:
        # ------------------ Define Query Tasks ------------------ #
        @run_in_new_session
        def get_active_leads(db):
            last_24h = datetime.now(timezone.utc) - timedelta(hours=24)
            return (
                db.query(func.count(func.distinct(LeadActivity.lead_id)))
                .filter(LeadActivity.activity_timestamp >= last_24h)
                .scalar()
                or 0
            )

        @run_in_new_session
        def get_hot_leads(db):
            data = (
                db.query(
                    LeadActivity.status,
                    func.count(func.distinct(LeadActivity.lead_id)).label("count")
                )
                .filter(LeadActivity.activity_timestamp >= last_24h)
                .group_by(LeadActivity.status)
                .all()
            )
            return {status: count for status, count in data}


        @run_in_new_session
        def get_avg_response_rate(db):
            rates = db.query(LeadPerformanceMetrics.response_rate).all()
            return (
                round(sum([x.response_rate or 0 for x in rates]) / len(rates), 2)
                if rates else 0
            )

        @run_in_new_session
        def get_avg_conversion_rate(db):
            data = (
                db.query(ExecutivePerformanceAggregates.conversion_rate)
                .filter(ExecutivePerformanceAggregates.period_start >= last_24h)
                .all()
            )
            return (
                round(sum([x.conversion_rate or 0 for x in data]) / len(data), 2)
                if data else 0
            )

        @run_in_new_session
        def get_no_response_24h(db):
            return (
                db.query(LeadActivity)
                .filter(
                    LeadActivity.status != "Closed",
                    LeadActivity.activity_timestamp < now - timedelta(hours=24),
                )
                .all()
            )

        @run_in_new_session
        def get_high_value_leads(db):
            return (
                db.query(LeadPerformanceMetrics)
                .filter(LeadPerformanceMetrics.conversion_probability >= 0.8)
                .all()
            )

        @run_in_new_session
        def get_status_counts(db):
            return (
                db.query(LeadActivity.status, func.count(LeadActivity.id))
                .group_by(LeadActivity.status)
                .all()
            )

        @run_in_new_session
        def get_top_executives(db):
            return (
                db.query(
                    ExecutivePerformanceAggregates.executive_name,
                    ExecutivePerformanceAggregates.conversion_rate,
                    ExecutivePerformanceAggregates.converted_leads_count,
                    ExecutivePerformanceAggregates.active_leads_count,
                    ExecutivePerformanceAggregates.performance_score,
                    ExecutivePerformanceAggregates.avg_response_time_minutes,
                    ExecutivePerformanceAggregates.avg_efficiency_score,
                    ExecutivePerformanceAggregates.total_leads_handled,
                    ExecutivePerformanceAggregates.calculated_at,
                    ExecutivePerformanceAggregates.total_customer_calls,
                    ExecutivePerformanceAggregates.total_meaningful_followups,
                    ExecutivePerformanceAggregates.total_site_visits,
                )
                .filter(
                    and_(
                        ExecutivePerformanceAggregates.calculated_at >= last_24h,
                        ExecutivePerformanceAggregates.calculated_at <= now,
                    )
                )
                .order_by(ExecutivePerformanceAggregates.conversion_rate.desc())
                .all()
            )

        @run_in_new_session
        def get_ai_suggestions(db):
            return (
                db.query(PerformanceSuggestions)
                .filter(PerformanceSuggestions.is_implemented == False)
                .order_by(PerformanceSuggestions.created_at.desc())
                .limit(3)
                .all()
            )

        # ------------------ Run Concurrently ------------------ #
        (
            active_leads,
            get_hot_leads,
            avg_response_rate,
            avg_conversion_rate,
            no_response_24h,
            high_value_leads,
            status_counts,
            top_executives,
            ai_suggestions,
        ) = await asyncio.gather(
            asyncio.to_thread(get_active_leads),
            asyncio.to_thread(get_hot_leads),
            asyncio.to_thread(get_avg_response_rate),
            asyncio.to_thread(get_avg_conversion_rate),
            asyncio.to_thread(get_no_response_24h),
            asyncio.to_thread(get_high_value_leads),
            asyncio.to_thread(get_status_counts),
            asyncio.to_thread(get_top_executives),
            asyncio.to_thread(get_ai_suggestions),
        )

        # ------------------ Build Response Data ------------------ #
        quick_metrics = {
            "active_leads": active_leads,
            "hot_leads_24h": get_hot_leads,
            "response_rate": avg_response_rate,
            "conversion_rate": avg_conversion_rate,
        }

        alerts = []
        for lead in no_response_24h[:3]:
            alerts.append(f" Lead #{lead.lead_id} - No response in 24h")
        for lead in high_value_leads[:3]:
            alerts.append(f" Lead #{lead.lead_id} - High value, needs immediate call")
        if len(high_value_leads) > 3:
            alerts.append(f" {len(high_value_leads)} leads with 80%+ conversion probability")

        funnel_stages = ["New", "Contacted", "Qualified", "Converted"]
        funnel_data = {stage: 0 for stage in funnel_stages}
        total_new = 0
        for status, count in status_counts:
            if status in funnel_data:
                funnel_data[status] = count
                if status == "New":
                    total_new = count
        funnel_percentages = {}
        if total_new > 0:
            running_total = total_new
            for stage in funnel_stages:
                funnel_percentages[stage] = round(
                    (funnel_data[stage] / running_total) * 100, 1
                ) if running_total else 0
                running_total = funnel_data[stage] if funnel_data[stage] > 0 else running_total

        # Calculate last_sync
        last_sync = max(
            (exec.calculated_at for exec in top_executives if exec.calculated_at),
            default=None
        )
        last_sync_str = last_sync.isoformat() if last_sync else None

        top_exec_list = [
            {
                "name": exec.executive_name[:16],
                "conversion_rate": round(exec.conversion_rate, 1) if exec.conversion_rate else 0,
                "name": exec.executive_name,
                "total_leads": exec.total_leads_handled or 0,
                "performance_score": round(exec.performance_score or 0, 2),
                "avg_response_time": round(exec.avg_response_time_minutes or 0, 1),
                "total_calls": exec.total_customer_calls or 0,
                "total_followups": exec.total_meaningful_followups or 0,
                "total_site_visits": exec.total_site_visits or 0,
                "calculated": exec.calculated_at,
            }
            for exec in top_executives
        ]

        suggestion_texts = [s.suggestion_text for s in ai_suggestions]
        total_time = round(time.time() - start_time, 2)

        # ------------------ Final Response ------------------ #
        return {
            "status": "success",
            "title": "🎯 LEAD INTELLIGENCE HUB",
            "period": "📊 Last 24hr",
            "last_sync": last_sync_str,
            "quick_metrics": quick_metrics,
            "alerts": alerts,
            "lead_funnel": {
                "data": funnel_data,
                "percentages": funnel_percentages,
            },
            "top_executives": top_exec_list,
            "ai_suggestions": suggestion_texts,
            "performance_metrics": {"total_time": total_time},
        }

    except Exception as e:
        print(f"❌ Dashboard Error: {e}")
        return {
            "status": "error",
            "response": f"Dashboard failed: {str(e)}",
        }
from app.generic.lead_external import lead

@router.get("/sync_lead")
async def lead_sync():
    try:
        asyncio.create_task(lead())
        return {"status":True}
    except Exception as e:
        return {"status":False}

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from app.BrightpathAI.services.live_streaming import broadcaster
from fastapi.responses import StreamingResponse

@router.get("/events")
async def events():
    # await broadcaster.publish("Hello, world!")
    return StreamingResponse(
        broadcaster.listen(),
        media_type="text/event-stream"
    )