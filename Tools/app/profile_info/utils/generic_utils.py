
import re
import httpx
import asyncio
import datetime
import os, sys
import time
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from collections import defaultdict 
from datetime import datetime, timedelta
from app.db.database import SessionLocal
from app.model.message import Message
from sqlalchemy import or_
from app.model.audio_file_model import AudioFile
import traceback

import json

from app.model.lead_history import LeadHistory
from app.model.lead_details import LeadActivity
from app.model.lead_performance import LeadPerformanceMetrics, PerformanceSuggestions, ExecutivePerformanceAggregates
from app.model.message import Message
from app.model.audio_file_model import AudioFile

from app.db.database import SessionLocal, engine, Base


from sqlalchemy import text

from sqlalchemy import text
from app.generic.embedding import generate_embedding

from sqlalchemy import text
from decimal import Decimal
from datetime import datetime, date

import decimal
import json

from app.BrightpathAI.models.chat_history import UserChatMessage 
async def query_executer(query: str, params: dict = None):
    """
    Universal SQL executor that always returns consistent JSON-serializable format
    """
    try:
        # Convert Decimals to float for JSON serialization
        def convert_decimals(obj):
            if isinstance(obj, decimal.Decimal):
                return float(obj)
            elif isinstance(obj, dict):
                return {k: convert_decimals(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [convert_decimals(item) for item in obj]
            else:
                return obj

        query_type = query.strip().split()[0].lower()
        
        if query_type == "select":
            with engine.begin() as conn:
                result = conn.execute(text(query), params or {})
                rows = result.mappings().all()
                
                # Convert to dict and handle Decimals
                data = [dict(r) for r in rows]
                data = convert_decimals(data)
                
                # ALWAYS return this consistent format
                return {
                    "query": query,
                     "data": data,
                }
                
        else:  # insert, update, delete
            with engine.begin() as conn:
                result = conn.execute(text(query), params or {})
                
                # ALWAYS return this consistent format  
                return {
                    # "success": True
                    "query": query,
                    "rows_affected": result.rowcount,
                    
                }

    except Exception as e:
        import traceback
        print("❌ SQL Execution Error:", e)
        print(traceback.format_exc())
        return {
            "success": False,
            "error": str(e),
            "type": "error"
        }

async def semantic_query_executor(
    query: str,
    table_name: str = "audio_files",
    text_column: str = "transcribed_text",
    embedding_column: str = "embedding",
    limit: int = 10
):
    """
    Atomic semantic search executor - doesn't depend on query_executer
    """
    try:
        # Generate embedding for the query
        query_vec = await generate_embedding(query)
        
        # Build semantic search SQL
        sql = f"""
            SELECT id, {text_column}, 
                   1 - ({embedding_column} <=> CAST(:query_vec AS vector)) AS similarity
            FROM {table_name}
            WHERE {embedding_column} IS NOT NULL
            AND {text_column} IS NOT NULL
            AND LENGTH({text_column}) > 0
            ORDER BY {embedding_column} <=> CAST(:query_vec AS vector)
            LIMIT :limit
        """

        params = {
            "query_vec": query_vec,
            "limit": limit
        }

        # Execute directly without query_executer
        with engine.begin() as conn:
            result = conn.execute(text(sql), params)
            rows = result.mappings().all()
            
            return {
                "table": table_name,
                "data": [dict(r) for r in rows]
            }
            
    except Exception as e:
        import traceback
        print(f"❌ Semantic search error: {e}")
        print(traceback.format_exc())
        return {
            "success": False, 
            "error": str(e),
            "query": query,
            "table": table_name
        }

import asyncio

import asyncio

async def test_query():

    user_query = "Find calls where customer talked about pricing concerns"

    # Call the semantic search executor
    result = await semantic_query_executor(
        query=user_query,
        table_name="audio_files",          
        text_column="transcribed_text",   
        embedding_column="transcript_embedding",      
        limit=10
    )

    print("🔍 Semantic Query Result:\n", result)


# if __name__ == "__main__":
#     asyncio.run(test_query())


HEADERS = {"Authorization": "demo_token_123"}

async def api_fetcher(url, headers=HEADERS):
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers)
            if response.status_code == 404:
                print(f"404 Not Found: {url}")
                return None
            elif response.status_code != 200:
                print(f"Unexpected status {response.status_code} for {url}")
                return None
            return response.json()
        except httpx.RequestError as e:
            print(f"Network error for {url}: {e}")
            return None
        except Exception as e:
            print(f"Unexpected API error for {url}: {e}")
            return None

async def whatsapp_msg_formatter(phone):
    if phone:
        content_parts = []
        db = SessionLocal()
        try:
            all_messages = (
                db.query(Message)
                .filter(Message.cx_number == phone)
                .order_by(Message.timestamp.asc())
                .all()
            )

            if all_messages:
                messages_by_admin = {}
                for msg in all_messages:
                    admin_num = msg.admin_number
                    messages_by_admin.setdefault(admin_num, []).append(msg)

                for admin_number, messages in messages_by_admin.items():
                    start_time = messages[0].timestamp.strftime("%Y-%m-%d %H:%M")
                    end_time = messages[-1].timestamp.strftime("%Y-%m-%d %H:%M")

                    content_parts.append(
                        f"\nWhatsApp Messages (admin:{admin_number} → cx:{phone}) [start: {start_time}]"
                    )

                    for msg in messages:
                        if msg.content and msg.content.strip():
                            direction = "→" if msg.direction == "outgoing" else "←"
                            whatsapp_text = f"{direction} {msg.content} {msg.timestamp.strftime('%Y-%m-%d %H:%M')}"
                            content_parts.append(whatsapp_text)

                    content_parts.append(f"(end: {end_time})")

        except Exception as e:
            print(f"Error fetching messages for phone {phone}: {e}")
        finally:
            db.close()
        return content_parts

async def call_logs_db(phone_number):
    db = SessionLocal()
    try:
        content_parts = []
        phone = phone_number

        # Query for call logs
        sales_call_logs = (
            db.query(AudioFile)
            .filter(
                or_(
                    AudioFile.ph_num.like(f"%{phone[-10:]}"),
                    AudioFile.ph_num.like(f"%{phone}%")
                )
            )
            .order_by(AudioFile.call_date.asc())
            .all()
        )

        if not sales_call_logs:
            return {"format_content": None,
            "call_data": None}
        try:

            update_count = (
                db.query(AudioFile)
                .filter(
                    # and_(
                        or_(
                            AudioFile.ph_num.like(f"%{phone[-10:]}"),
                            AudioFile.ph_num.like(f"%{phone}%")
                        ),
                        
                        AudioFile.status == 0, 
                        AudioFile.file_url.isnot(None),  
                        AudioFile.transcribed_text.is_(None)  
                    # )
                )
                .update(
                    {AudioFile.status: 1},
                    synchronize_session=False
                )
            )
            db.commit()
            print(f"Updated {update_count} records for phone {phone}")
            db.commit()
            print(f"Updated {update_count} records for phone {phone} (status 0 → 1)")
            
        except Exception as update_error:
            print(f"Error updating records: {update_error}")
            traceback.print_exc()
            db.rollback()

        content_parts.append("team: callType: cx_no: duration: date:")
        for call in sales_call_logs:
            call_date = (
                call.call_date.strftime("%Y-%m-%d %H:%M")
                if call.call_date else "no date"
            )
            
            admin_team = getattr(call, "admin_Team", "Unknown") or "Unknown"
            username = call.username or ""
            call_type = call.call_type or ""
            ph_num = call.ph_num or " "
            call_duration = call.call_duration or 0
            status = call.status or ""
            trascript=call.transcribed_text or ""

            if call_type.lower() == "missed":
                log = f"{admin_team} {username} {call_type} from {ph_num} on {call_date}"
            else:
                log = f"{admin_team} {username} {call_type} from {ph_num} for {call_duration}s on {call_date}\n call trasncript:{trascript}"

            content_parts.append(log)

        return {
            "format_content": content_parts,
            "call_data": sales_call_logs
        }

    except Exception as e:
        print(f"Error fetching call logs for phone {phone_number}: {e}")
        traceback.print_exc()
        return {"format_content": [], "call_data": []}

    finally:
        db.close()
SALES_EXECUTIVES = [
        "sagarikanoatia905",
        "harish99",
        "abbas24042000",
        "ashwathianair",
        "hari.kattamanchi",
    ]

async def call_log_api(phone_number):
    content_parts = []
    try:
        if phone_number is None:
            content_parts.append("Phone number is missing.")
            return {
                "call_log_api": None,
                "format_content": content_parts
            }

        phone = phone_number
        api_url = f"https://www.rentmystay.com/T/call_log_by_number/{phone}"
        api_data = await api_fetcher(api_url)

        if (
            isinstance(api_data, dict)
            and api_data.get("msg") == "Success"
            and isinstance(api_data.get("data"), dict)
        ):
            call_logs = api_data["data"].get("call_details", [])
            print(f"Extracted call logs: {call_logs}")

            if call_logs and isinstance(call_logs, list):
                content_parts.append("\nSales_team: callType: cx_no: duration: date")
                for log in call_logs:
                    if isinstance(log, dict):
                        call_date = log.get("callDate", "")
                        call_type = log.get("callType", "")
                        username = log.get("username", "")
                        duration = log.get("callDuration", "0")
                        ph_num = log.get("phNum", phone)
                        team_role = "sales" if username.lower() in SALES_EXECUTIVES else "caretaker"
                        if call_type.lower() == "missed":
                            log_entry = f"{team_role} {username} received {call_type} from {ph_num} on {call_date}"
                        else:
                            log_entry = f"{team_role} {username} {call_type} {ph_num} for {duration} sec on {call_date}"

                        content_parts.append(log_entry)
                    else:
                   
                        content_parts.append(str(log))
            else:
                content_parts.append("Sales Call Logs: No call logs found in external API.")
        else:
            content_parts.append("Sales Call Logs: Unexpected API response format.")
        
        print("content_parts", content_parts)
        return {
            "call_log_api": api_data,
            "format_content": content_parts
        }

    except Exception as e:
        print(f"Error fetching call logs for phone {phone_number}: {e}")
        traceback.print_exc()
        return {
            "call_log_api": None,
            "format_content": [f"Error: {str(e)}"]
        }


# import asyncio
# from app.model.chat_history import  UserChatMessage
# if __name__ == "__main__":
#     import asyncio

#     phone_number = "8147939641"

#     result = asyncio.run(call_logs_db(phone_number))
    
#     print("=== CALL LOGS RESULTS ===")
#     if result:
#         print("Formatted content:")
#         for line in result["format_content"]:
#             print(line)
#         print(f"\nNumber of call records: {len(result['call_data'])}")
#     else:
#         print("No call logs found")
from sqlalchemy import desc
async def get_season_data(season_id: str):
    try:
        db: Session = SessionLocal()

        season_data = (
            db.query(UserChatMessage.ans)
            .filter(UserChatMessage.chat_session_id == season_id)
            .order_by(desc(UserChatMessage.id))  
            .limit(1)  
            .all()
        )

        if season_data:
            
            return [r.ans for r in reversed(season_data)]
        else:
            return None

    except Exception as e:
        print(f"Error fetching chat history for session {season_id}: {e}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        db.close()

# if __name__ == "__main__":
#     import asyncio

#     season_id = None

#     result = asyncio.run(get_season_data(season_id))
    
#     print("=== CHAT HISTORY RESULTS  
#     if result:
#         for idx, ans in enumerate(result, 1):
#             print(f"Message {idx}: {ans}")
#     else:
#         print("No chat history found for the given session ID.")

from app.model.whatsapp_chats import WhatsappChatSession
async def whatsapp_msg_migrate():
    try:
        db = SessionLocal()

        # UNIQUE (admin_number, cx_number)
        all_pairs = (
            db.query(Message.admin_number, Message.cx_number)
            # .filter(Message.cx_number=="919989971586")
            .distinct()
            .all()
        )

        for admin_number, cx_number in all_pairs:

            all_messages = (
                db.query(Message)
                .filter(
                    Message.admin_number == admin_number,
                    Message.cx_number == cx_number
                )
                .order_by(Message.timestamp.asc())
                .all()
            )

            concatenated_content = "\n".join(
                f"{'→' if msg.direction == 'outgoing' else '←'} "
                f"{msg.content} {msg.timestamp.strftime('%Y-%m-%d %H:%M')}"
                for msg in all_messages
                if msg.content and msg.content.strip()
            )

            start_time = all_messages[0].timestamp if all_messages else None
            end_time = all_messages[-1].timestamp if all_messages else None

            # print( # f"CX Number: {cx_number_value}\n" # f"{concatenated_content}\n" # f"Admin Phone: {admin_phone}, Customer Phone: {customer_phone}\n" # f"Start Time: {start_time}, End Time: {end_time}\n" # f"{'-'*40}\n" # )
            print(
                f" Admin: {admin_number}, CX: {cx_number}, "
                f"Messages: {concatenated_content}, \n Start: {start_time},\n End: {end_time}"
            )
            chat_session = WhatsappChatSession(
                customer_phone=cx_number,
                admin_phone=admin_number,
                start_time=start_time,
                end_time=end_time,
                conversation_summary=concatenated_content
            )

            db.add(chat_session)
            db.commit()

    except Exception as e:
        print(f"Error migrating messages: {e}")
    finally:
        db.close()



if __name__ == "__main__":
    import asyncio
    asyncio.run(whatsapp_msg_migrate())
