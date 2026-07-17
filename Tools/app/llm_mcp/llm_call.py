# # from google.genai import types 
# # import asyncio
# # import os, sys
# # import json
# # from typing import Dict, Any, List
# # import time

# # sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
# # from generic.external_data import get_call, get_ticket, get_invoice, get_communication, get_booking, get_lead, get_faq_policy_search
# # from google import genai

# # function_declarations = [
# #     {
# #         "name": "get_invoice",
# #         "description": "Fetch invoice data for a booking including amounts, status, due date etc.",
# #         "parameters": {
# #             "type"  : "object",
# #             "properties": {
# #                 "booking_id": {"type": "string", "description": "Booking ID"},
# #             },
# #             "required": ["booking_id"]
# #         }
# #     },
# #     {
# #         "name": "get_booking",
# #         "description": "Fetch booking details including dates, status, customer information etc.",
# #         "parameters": {
# #             "type": "object",
# #             "properties": {
# #                 "booking_id": {"type": "string", "description": "Booking ID"},
# #             },
# #             "required": ["booking_id"]
# #         }
# #     },
# #     {
# #         "name": "get_communication",
# #         "description": "Fetch communication history for a booking",
# #         "parameters": {
# #             "type": "object",
# #             "properties": {
# #                 "booking_id": {"type": "string", "description": "Booking ID"},
# #             },
# #             "required": ["booking_id"]
# #         }
# #     },
# #     {
# #         "name": "get_call",
# #         "description": "Fetch call log for a booking",
# #         "parameters": {
# #             "type" : "object",
# #             "properties": {
# #                 "booking_id": {"type": "string", "description": "Booking ID"},
# #             },
# #             "required": ["booking_id"]
# #         }
# #     },
# #     {
# #         "name": "get_ticket",
# #         "description": "Fetch ticket data for a booking",
# #         "parameters": {
# #             "type": "object",
# #             "properties": {
# #                 "booking_id": {"type": "string", "description": "Booking ID"}, 
# #             },
# #             "required": ["booking_id"]
# #         }
# #     },
# #     {
# #         "name": "get_lead",
# #         "description": "Fetch lead data for a lead id",
# #         "parameters": {
# #             "type": "object",
# #             "properties": {
# #                 "lead_id": {"type": "string", "description": "Lead ID"},
# #             },
# #             "required": ["lead_id"]
# #         }
# #     },
# #     {
# #         "name": "get_faq_policy_search",
# #         "description": "Search for FAQs based on user query or company policies",
# #         "parameters": {
# #             "type": "object",
# #             "properties": {
# #                 "query": {
# #                     "type": "string",
# #                     "description": "User query or company policy"
# #                 },
# #                 "top_k": {
# #                     "type": "integer",
# #                     "description": "Number of top similar FAQs to return",
# #                     "default": 2
# #                 }
# #             },
# #             "required": ["query"]
# #         }
# #     }
# # ]

# # client = genai.Client(api_key="AIzaSyAAF1VQ_Qf-kus35soRZK-x-WJq3l9u2nQ")
# # tools = types.Tool(function_declarations=function_declarations)

# # async def llm_func_call(query: str, system_prompt: str = "support", booking_id: str = None, lead_id: str = None) -> str:
# #     """
# #     Enhanced LLM call with MAX 10 function calls limit
# #     """
# #     total_start_time = time.time()
# #     print(f"Starting LLM function call at {time.strftime('%H:%M:%S')}")
    
# #     system_message = f"""
# #     Your role: {system_prompt}
    
# #     IMPORTANT INSTRUCTIONS:
# #     1. Analyze the user query and call necessary functions to get complete information
# #     2. MAXIMUM 10 function calls allowed - prioritize the most important ones
# #     3. If user asks for lead details, use get_lead function
# #     4. If user asks for booking details, use get_booking function
# #     5. After getting function results, provide a comprehensive, direct answer
# #     6. Be concise but informative
# #     7. Available default IDs: {f"Booking ID: {booking_id}" if booking_id else ""} {f"Lead ID: {lead_id}" if lead_id else ""}
# #     """
    
# #     contents = [
# #         types.Content(role="user", parts=[types.Part(text=system_message)]),
# #         types.Content(role="user", parts=[types.Part(text=query)])
# #     ]

# #     config = types.GenerateContentConfig(
# #         tools=[tools],
# #         tool_config=types.ToolConfig(
# #             function_calling_config=types.FunctionCallingConfig(mode="ANY")
# #         )
# #     )

# #     llm_detection_start = time.time()
# #     resp = client.models.generate_content(
# #         model="gemini-2.5-flash", 
# #         contents=contents,
# #         config=config
# #     )
# #     llm_detection_time = time.time() - llm_detection_start
# #     print(f"=> LLM Function Detection Time: {llm_detection_time:.2f}s")

# #     # Check if response has candidates and content
# #     if not resp.candidates or not resp.candidates[0].content:
# #         total_time = time.time() - total_start_time
# #         print(f"❌ No response content from LLM after {total_time:.2f}s")
# #         return "I apologize, but I couldn't process your request. Please try again."

# #     function_calls = []
# #     direct_response = None

# #     for part in resp.candidates[0].content.parts:
# #         if hasattr(part, 'function_call') and part.function_call:
# #             function_calls.append(part.function_call)
# #         elif hasattr(part, 'text') and part.text:
# #             direct_response = part.text

# #     if not function_calls and direct_response:
# #         total_time = time.time() - total_start_time
# #         print(f"=> LLM Direct Response Total Time: {total_time:.2f}s")
# #         return direct_response

# #     if len(function_calls) > 10:
# #         print(f"  Function call limit exceeded: {len(function_calls)} > 10, truncating to first 10")
# #         function_calls = function_calls[:10]
# #         print(f" Remaining function calls: {[fc.name for fc in function_calls]}")

# #     # Enhanced auto-fill logic to exclude FAQ search from booking_id auto-fill
# #     for fc in function_calls:
# #         if fc.name == "get_lead" and (not hasattr(fc, 'args') or 'lead_id' not in fc.args or not fc.args.get('lead_id')):
# #             if lead_id:
# #                 if not hasattr(fc, 'args') or fc.args is None:
# #                     fc.args = {}
# #                 fc.args['lead_id'] = lead_id
# #                 print(f"Auto-filled lead_id: {lead_id} for get_lead function")
        
# #         # Don't auto-fill booking_id for FAQ search function
# #         elif fc.name not in ["get_lead", "get_faq_policy_search"] and (not hasattr(fc, 'args') or 'booking_id' not in fc.args or not fc.args.get('booking_id')):
# #             if booking_id:
# #                 if not hasattr(fc, 'args') or fc.args is None:
# #                     fc.args = {}
# #                 fc.args['booking_id'] = booking_id
# #                 print(f"Auto-filled booking_id: {booking_id} for {fc.name} function")

# #     # Check for missing required parameters
# #     missing_ids = []
# #     for fc in function_calls:
# #         if fc.name == "get_lead" and (not hasattr(fc, 'args') or 'lead_id' not in fc.args or not fc.args.get('lead_id')):
# #             missing_ids.append("lead_id")
# #         elif fc.name not in ["get_lead", "get_faq_policy_search"] and (not hasattr(fc, 'args') or 'booking_id' not in fc.args or not fc.args.get('booking_id')):
# #             missing_ids.append("booking_id")
# #         elif fc.name == "get_faq_policy_search" and (not hasattr(fc, 'args') or 'query' not in fc.args or not fc.args.get('query')):
# #             missing_ids.append("query for FAQ search")
    
# #     if missing_ids:
# #         missing_str = " and ".join(set(missing_ids))
# #         total_time = time.time() - total_start_time
# #         print(f" Missing IDs Response Time: {total_time:.2f}s")
# #         return f"I need {missing_str} to fetch the details. Please provide {missing_str}."

# #     async def execute_function_call(fc):
# #         func_start_time = time.time()
# #         try:
# #             print(f"=> Executing {fc.name} with args: {fc.args}")
            
# #             # Clean arguments - remove booking_id from FAQ search if present
# #             args = dict(fc.args) if hasattr(fc, 'args') and fc.args else {}
# #             if fc.name == "get_faq_policy_search" and "booking_id" in args:
# #                 print(f"  Removing booking_id from FAQ search arguments")
# #                 del args["booking_id"]
            
# #             if fc.name == "get_invoice":
# #                 result = await get_invoice(**args)
# #             elif fc.name == "get_booking":
# #                 result = await get_booking(**args)
# #             elif fc.name == "get_communication":
# #                 result = await get_communication(**args)
# #             elif fc.name == "get_call":
# #                 result = await get_call(**args)
# #             elif fc.name == "get_ticket":
# #                 result = await get_ticket(**args)
# #             elif fc.name == "get_lead": 
# #                 result = await get_lead(**args)
# #             elif fc.name == "get_faq_policy_search":
# #                 result = await get_faq_policy_search(**args)
# #             else:
# #                 result = {"error": f"Unknown function {fc.name}"}
                
# #             func_time = time.time() - func_start_time
# #             print(f" {fc.name} executed in {func_time:.2f}s")
# #             return result
            
# #         except Exception as e:
# #             func_time = time.time() - func_start_time
# #             print(f"❌ {fc.name} failed after {func_time:.2f}s: {str(e)}")
# #             return {"error": f"Function {fc.name} failed: {str(e)}"}

# #     parallel_start = time.time()
# #     tasks = [execute_function_call(fc) for fc in function_calls]
# #     function_results = await asyncio.gather(*tasks, return_exceptions=True)
# #     parallel_time = time.time() - parallel_start
# #     print(f"=> All functions parallel execution time: {parallel_time:.2f}s")

# #     function_responses_content = [
# #         types.Content(role="user", parts=[types.Part(text=query)])
# #     ]

# #     for fc, result in zip(function_calls, function_results):
# #         function_responses_content.extend([
# #             types.Content(role="model", parts=[types.Part(function_call=fc)]),
# #             types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
# #                 name=fc.name,
# #                 response=result if not isinstance(result, Exception) else {"error": str(result)}
# #             ))])
# #         ])

# #     final_config = types.GenerateContentConfig(temperature=0.1)
    
# #     try:
# #         llm_response_start = time.time()
# #         final_resp = client.models.generate_content(
# #             model="gemini-2.5-flash",
# #             contents=function_responses_content,
# #             config=final_config
# #         )
# #         llm_response_time = time.time() - llm_response_start
# #         print(f" ==> Final LLM Response Generation Time: {llm_response_time:.2f}s")

# #         print("Function calls:", [fc.name for fc in function_calls])
# #         print("Function results:", function_results)
        
# #         # Safe extraction of final response text
# #         if (final_resp and final_resp.candidates and 
# #             final_resp.candidates[0].content and 
# #             final_resp.candidates[0].content.parts):
            
# #             for part in final_resp.candidates[0].content.parts:
# #                 if hasattr(part, 'text') and part.text:
# #                     total_time = time.time() - total_start_time
# #                     print(f" TOTAL LLM Function Call Time: {total_time:.2f}s")
# #                     # results=[]
# #                     # results .append("all_data":function_results)
# #                     # results.append(part.text)
# #                     return part.text
# #         else:
# #             # If no proper response, create fallback
# #             print("⚠️ No valid response from final LLM, using fallback")
# #             return create_fallback_response(function_calls, function_results)
                
# #     except Exception as e:
# #         llm_response_time = time.time() - llm_response_start
# #         print(f"❌ Final LLM failed after {llm_response_time:.2f}s: {e}")
# #         total_time = time.time() - total_start_time
# #         print(f" TOTAL LLM Function Call Time (with error): {total_time:.2f}s")
# #         return create_fallback_response(function_calls, function_results)

# #     total_time = time.time() - total_start_time
# #     print(f"  TOTAL LLM Function Call Time (no response): {total_time:.2f}s")
# #     return create_fallback_response(function_calls, function_results)

# # def create_fallback_response(function_calls: List, function_results: List) -> str:
# #     """Create a simple response when LLM fails to generate one"""
# #     responses = []
# #     for fc, result in zip(function_calls, function_results):
# #         if not isinstance(result, Exception) and result:
# #             responses.append(f"{fc.name.replace('_', ' ').title()}: {str(result)}")
    
# #     if responses:
# #         return "Here's the information I found:\n" + "\n".join(responses)
# #     else:
# #         return "I tried to fetch the information but didn't get any results."

# # # Fixed get_faq_policy_search function
# # # async def get_faq_policy_search(query: str, top_k: int = 2): 
# # #     """
# # #     Perform FAQ vector similarity search using pgvector.
# # #     Returns only question + answer.
# # #     """
# # #     from sqlalchemy import select
# # #     from app.db.database import SessionLocal
# # #     from app.generic.embedding import generate_embedding  # Make sure this function exists
    
# # #     db = SessionLocal()
# # #     try:
# # #         query_embedding = generate_embedding(query)

# # #         stmt = (
# # #             select(FAQ.question, FAQ.answer)
# # #             .order_by(FAQ.faq_vector.cosine_distance(query_embedding))
# # #             .limit(top_k)
# # #         )
# # #         result = db.execute(stmt)
# # #         faqs = result.all()

# # #         return [{"question": q, "answer": a} for q, a in faqs]
# # #     except Exception as e:
# # #         return {"error": f"FAQ search failed: {str(e)}"}
# # #     finally:
# # #         db.close()

# from google.genai import types 
# import asyncio
# import os, sys
# import json
# from typing import Dict, Any, List
# import time

# sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
# from generic.external_data import get_call, get_ticket, get_invoice, get_communication, get_booking, get_lead, get_faq_policy_search
# from app.services.cache_manager import cache_manager
# from google import genai
# from app.generic.lead_external import sales_executive_lead, sales_team_24hr
# from app.generic.generic_utils import query_executer, semantic_query_executor

# function_declarations = [
#     {
#         "name": "get_invoice",
#         "description": "Fetch invoice data for a booking including amounts, status, due date etc.",
#         "parameters": {
#             "type"  : "object",
#             "properties": {
#                 "booking_id": {"type": "string", "description": "Booking ID"},
#             },
#             "required": ["booking_id"]
#         }
#     },
#     {
#         "name": "get_booking",
#         "description": "Fetch booking details including dates, status, customer information etc.",
#         "parameters": {
#             "type": "object",
#             "properties": {
#                 "booking_id": {"type": "string", "description": "Booking ID"},
#             },
#             "required": ["booking_id"]
#         }
#     },
#     {
#         "name": "get_communication",
#         "description": "Fetch communication history for a booking",
#         "parameters": {
#             "type": "object",
#             "properties": {
#                 "booking_id": {"type": "string", "description": "Booking ID"},
#             },
#             "required": ["booking_id"]
#         }
#     },
#     {
#         "name": "get_call",
#         "description": "Fetch call log for a booking",
#         "parameters": {
#             "type" : "object",
#             "properties": {
#                 "booking_id": {"type": "string", "description": "Booking ID"},
#             },
#             "required": ["booking_id"]
#         }
#     },
#     {
#         "name": "get_ticket",
#         "description": "Fetch ticket data for a booking",
#         "parameters": {
#             "type": "object",
#             "properties": {
#                 "booking_id": {"type": "string", "description": "Booking ID"}, 
#             },
#             "required": ["booking_id"]
#         }
#     },
#     {
#         "name": "get_lead",
#         "description": "Fetch lead data for a lead id",
#         "parameters": {
#             "type": "object",
#             "properties": {
#                 "lead_id": {"type": "string", "description": "Lead ID"},
#             },
#             "required": ["lead_id"]
#         }
#     },
#     {
#         "name": "get_faq_policy_search",
#         "description": "Search for FAQs based on user query or company policies importent call this in excepption case only, like related to policy or faqs",
#         "parameters": {
#             "type": "object",
#             "properties": {
#                 "query": {
#                     "type": "string",
#                     "description": "User query or company policy"
#                 },
#                 "top_k": {
#                     "type": "integer",
#                     "description": "Number of top similar FAQs to return",
#                     "default": 2
#                 }
#             },
#             "required": ["query"]
#         }
#     },
#    {
#         "name": "sales_executive_lead",
#         "description": "Fetch all lead data handled by a sales executive within the last 24 hours for performance analysis",
#         "parameters": {
#             "type": "object",
#             "properties": {
#                 "executive_name": {
#                     "type": "string",
#                     "description": "Sales executive username (e.g. 'Sagarikanoatia905', 'harish99', 'ashwathianair', 'hari.kattamanchi','abbas24042000' )"
#                 }
#             },
#             "required": ["executive_name"]
#         }
#     },
#         {
#         "name": "sales_team_24hr",
#         "description": "Fetch lead data for ALL sales executives in the team from the last 24 hours for team-wide performance analysis",
#         "parameters": {
#             "type": "object",
#             "properties": {
#                 # No parameters needed since it gets data for entire team
#             }
#         }
#     },

#     { 
#     "name": "query_executer",
#     "description": """

#    IMPORTANT SQL RULES FOR LLM (VERY STRICT):
#    Please always use the correct table names and column names exactly as defined in the database schema.
#    always read the table schema and column names before writing query.  

# 1. **Always use exact numeric values directly in SQL.**
#    - ❌ DO NOT use variables like :customer_phone, :param_name, %(phone)s
#    - ✔ ALWAYS write:  RIGHT(m.cx_number, 10) = '6362272958'

# 2. **Never produce parameterized queries.**
#    Always return a fully hardcoded SQL query.

# 3. **Never use SELECT *.**
#    Always specify each column explicitly.

# 4. **Queries must be valid PostgreSQL.**

# 5. **Always use the correct table names and column names exactly as defined.**

# 6. **When combining WhatsApp + Call logs, ALWAYS follow this pattern:**
# ******BELOW IS JUST AN EXAMPLE FORMAT, BUT MAKE SURE FIRST READ TABLE SCHEMA AND COLUMN NAMES AND THEN WRITE THE QUERY ACCORDINGLY.

# ** alwasy exclude the group conversation utill the not mentioned in the query.  group number like(120363028135833460 )**
# (
#     SELECT 
#         m.timestamp AS interaction_time,
#         'whatsapp' AS channel,
#         m.direction AS interaction_direction,
#         m.content AS interaction_text,
#         NULL AS call_duration,
#         NULL AS agent_username
#     FROM messages m
#     WHERE RIGHT(m.cx_number, 10) = '6362272958'
# )
# UNION ALL
# (
#     SELECT
#         a.call_datetime AS interaction_time,
#         'call' AS channel,
#         a.call_type AS interaction_direction,
#         a.transcript_text AS interaction_text,
#         a.call_duration AS call_duration,
#         a.emp_name AS agent_username
#     FROM call_recordings_transcript a
#     WHERE RIGHT(a.customer_phone_number, 10) = '6362272958'
# )
# ORDER BY interaction_time ASC;

# 7. **When user gives a phone number, ALWAYS insert that number directly in SQL.**

# 8. **Use the function call format:**

# query_executer(query="YOUR SQL QUERY", params={})
# 9 analyse the query 
# e.ge if question like (which customer called regarding the issues .. any problem ) then use some word like below 
# 'payment': ['payment', 'paid', 'money', 'refund', 'invoice', 'bill', 'charge', 'cash', 'bank', 'transaction'],
#         'technical': ['login', 'password', 'website', 'app', 'error', 'bug', 'crash', 'not working', 'slow', 'technical'],
#         'booking': ['booking', 'reservation', 'book', 'reserve', 'schedule', 'appointment', 'slot'],
#         'cancellation': ['cancel', 'cancellation', 'refund', 'terminate', 'stop', 'end'],
#         'service': ['service', 'quality', 'bad', 'poor', 'complaint', 'issue', 'problem', 'dissatisfied'],
#         'delivery': ['delivery', 'shipping', 'dispatch', 'arrive', 'receive', 'ship', 'deliver'],
#         'product': ['product', 'item', 'quality', 'defective', 'damaged', 'broken', 'faulty'],
#         'support': ['support', 'help', 'assistance', 'contact', 'representative', 'agent'],
#         'account': ['account', 'profile', 'details', 'information', 'update', 'change', 'modify']

#  10 ** write query in such away so limit not exceeds.**
# Schemas:

# 1️ **CallRecordingsTranscript (call_recordings_transcript) - Call Logs (UPDATED)**  
#     - id SERIAL PRIMARY KEY  
#     - emp_phone_number VARCHAR  
#     - emp_name VARCHAR  (may be sales team, support , finace team etc.)
#     - customer_phone_number VARCHAR  
#     - call_datetime TIMESTAMP NOT NULL  
#     - call_duration INTEGER  
#     - call_type VARCHAR  
#     - department VARCHAR  
#     - audio_url VARCHAR  
#     - transcript_text VARCHAR  
#     - filename VARCHAR  
#     - uploaded_at TIMESTAMP  
#     - status INTEGER DEFAULT 0  
#     - call_id VARCHAR(100)  
#     - transcript_embedding VECTOR(384)
#     - intent TEXT  
#     - emotion TEXT  
#     - tone TEXT  
#     - action_layer TEXT  
#     - context TEXT  
#     - outcome TEXT  
#     - language TEXT  
  
    
# 2️**LeadActivity (lead_activities_details) - Leads**

# id                             SERIAL PRIMARY KEY
# lead_id                        VARCHAR(100) NOT NULL         -- e.g., "LD_2024_5582"
# customer_phone                 VARCHAR(20)                    -- e.g., "9876543210"
# customer_phone2                VARCHAR(20)                    -- e.g., "9123456780"
# customer_email                 VARCHAR(200)                   -- e.g., "john.doe@gmail.com"
# location                       VARCHAR(200)                   -- e.g., "Bangalore - Whitefield"
# origin                         VARCHAR(100)                   -- e.g., "Google Ads"
# status                         VARCHAR(50)                    -- e.g., "Active", "Booked", "Ignored", "Waiting"
# assigned_to                    VARCHAR(100)                   -- e.g., "emp_1023"
# added_by                       VARCHAR(100)                   -- e.g., "admin_user"
# added_on                       TIMESTAMP                      -- e.g., "2025-02-10 14:32:00"
# closed_on                      TIMESTAMP                      -- e.g., "2025-02-15 18:10:00"
# last_updated_by                VARCHAR(100)                   -- e.g., "emp_4321"
# followups                      JSONB                          -- e.g., [{"date":"2025-02-11","note":"Called customer"}]
# content                        TEXT NOT NULL                  -- e.g., "Customer wants a 2BHK in Whitefield..."
# embedding                      VECTOR(384)                    -- e.g., [0.023, -0.553, ... 384 dims]
# extracted_data                 JSONB                          -- e.g., {"budget":"30k","bhk":"2BHK"}
# activity_timestamp             TIMESTAMP                      -- e.g., "2025-02-10 14:30:00"
# performance_metrics_calculated BOOLEAN                        -- e.g., true / false
# last_metrics_calculation       TIMESTAMP                      -- e.g., "2025-02-10 15:00:00"

# 3️ **WhatsApp Message (messages) - WhatsApp** this is master table for whatsapp messages
# id                         SERIAL PRIMARY KEY
# message_id                 VARCHAR(255) NOT NULL           -- e.g., "ABCD1234XYZ@c.us"
# admin_number               VARCHAR(100) NOT NULL           -- e.g., "+919876543210"
# cx_number                  VARCHAR(100) NOT NULL           -- e.g., "+918888777766"
# content                    VARCHAR                         -- e.g., "Hi, I'm looking for a 2BHK"
# clean_content              VARCHAR                         -- e.g., "looking for 2 bhk"
# direction                  VARCHAR(20) NOT NULL            -- e.g., "incoming" or "outgoing"
# message_type               VARCHAR(50)                     -- e.g., "text", "image"
# timestamp                  TIMESTAMP                       -- e.g., "2025-02-10 14:52:00"
# last_sync                  TIMESTAMP                       -- e.g., "2025-02-10 14:55:00"
# media                      BOOLEAN                         -- e.g., false
# device                     VARCHAR(50)                     -- e.g., "android"
# from_me                    BOOLEAN                         -- e.g., true
# remote_jid                 VARCHAR(255)                    -- e.g., "918888777766@c.us"
# participant                VARCHAR(255)                    -- e.g., null (not group)
# message_key_id             VARCHAR(255)                    -- e.g., "3EB0434C534A"
# additional_data            JSONB                           -- e.g., {"latency":120}
# clean_content_embedding    VECTOR(384)                     -- e.g., [0.12, -0.08, ...]


# this for storing whatsapp chat session summary and metadata in single row all chat of customer
# 4 **Table: whatsapp_chat_sessions

# id SERIAL PRIMARY KEY
# admin_phone VARCHAR(100) NOT NULL -- e.g., "917411146474"
# customer_phone VARCHAR(100) NOT NULL -- e.g., "919876543210"

# conversation_summary TEXT -- e.g., "→ Hello\n← Yes tell me\n→ I want 2BHK..."


# start_time TIMESTAMP -- e.g., "2025-02-10 09:15:00"
# end_time TIMESTAMP -- e.g., "2025-02-10 11:45:00"

# summary_embedding VECTOR(384) -- e.g., [0.112, -0.452, ... 384 dims]

# created_at TIMESTAMP NOT NULL -- e.g., "2025-02-10 12:00:55"


# Query Examples:
# - Call volume by agent:
#   SELECT emp_name, call_type, COUNT(*) as call_count, AVG(call_duration) as avg_duration 
#   FROM call_recordings_transcript
#   WHERE call_datetime >= NOW() - INTERVAL '7 days' 
#   GROUP BY emp_name, call_type;

# - Lead conversion rates:
#   SELECT assigned_to, status, COUNT(*) as lead_count,
#          ROUND(COUNT(CASE WHEN status = 'converted' THEN 1 END) * 100.0 / COUNT(*), 2) as conversion_rate
#   FROM lead_activities_details 
#   WHERE added_on >= NOW() - INTERVAL '30 days'
#   GROUP BY assigned_to, status;

# - WhatsApp response analysis:
#   SELECT admin_number, direction, COUNT(*) as message_count
#   FROM messages 
#   WHERE timestamp >= NOW() - INTERVAL '3 days'
#   GROUP BY admin_number, direction;

# - Cross-channel customer journey:
#   SELECT l.customer_phone, l.assigned_to, l.status,
#          COUNT(DISTINCT m.id) as message_count,
#          COUNT(DISTINCT a.id) as call_count
#   FROM lead_activities_details l
#   LEFT JOIN messages m ON l.customer_phone = m.cx_number
#   LEFT JOIN call_recordings_transcript a ON l.customer_phone = a.customer_phone_number
#   WHERE l.added_on >= NOW() - INTERVAL '7 days'
#   GROUP BY l.customer_phone, l.assigned_to, l.status;

# Always return the result using the `query_executer(query, params)` function.

# """,
#     "parameters": {
#         "type": "object",
#         "properties": {
#             "query": {
#                 "type": "string",
#                 "description": "The SQL query string to be executed using SQLAlchemy text() syntax."
#             },
#             "params": {
#                 "type": "object",
#                 "description": "Optional parameters dictionary for the SQL query (used for :param placeholders)."
#             }
#         },
#         "required": ["query"]
#     }
# },


# ]

# # context TEXT -- e.g., "Customer asking for property availability"
# # outcome TEXT -- e.g., "Site visit scheduled"
# # intent TEXT -- e.g., "Rental inquiry"
# # emotion TEXT -- e.g., "Neutral"
# # tone TEXT -- e.g., "Polite"
# # actionable_signal TEXT -- e.g., "Call back at 5 PM"
# # topic TEXT -- e.g., "2BHK rental discussion"

# # client = genai.Client(api_key="AIzaSyAAF1VQ_Qf-kus35soRZK-x-WJq3l9u2nQ")
# client = genai.Client(api_key="AIzaSyBK5gIuhObyv0VgFfkgn8ZdOFu5yLiN5ug")
# tools = types.Tool(function_declarations=function_declarations)

# async def llm_func_call(query: str, system_prompt: str = "support", booking_id: str = None, lead_id: str = None) -> str:
#     total_start_time = time.time()
#     query=query.strip()
#     print(f"Starting LLM function call at {time.strftime('%H:%M:%S')}")
    
#     if booking_id and any(word in query.lower() for word in ['cancel', 'cancellation', 'email']):
#         cancellation_emails = await cache_manager.get_emails_by_category(booking_id, 'cancellation')
#         if cancellation_emails:
#             print(f"🎯 Found {len(cancellation_emails)} cancellation emails in cache")
            
    
#     system_message = f"""
#     Your role: {system_prompt}
#     note:
#     first try to get using sql query if not then use function call
#     IMPORTANT INSTRUCTIONS:
#     1. Analyze the user query and call necessary functions to get complete information
#     2. MAXIMUM 10 function calls allowed - prioritize the most important ones
#     3. If user asks for lead details, use get_lead function
#     4. If user asks for booking details, use get_booking function
#     5. After getting function results, provide a comprehensive, direct answer
#     6. Be concise but informative
#     7. Available default IDs: {f"Booking ID: {booking_id}" if booking_id else ""} {f"Lead ID: {lead_id}" if lead_id else ""}
#     """
    
#     contents = [
#         # types.Content(role="user", parts=[types.Part(text=system_message)]),
#         types.Content(role="user", parts=[types.Part(text=query)])
#     ]

#     config = types.GenerateContentConfig(
#         tools=[tools],
#         tool_config=types.ToolConfig(
#             function_calling_config=types.FunctionCallingConfig(mode="ANY")
#         )
#     )

#     llm_detection_start = time.time()
#     # resp = client.models.generate_content(
#     #     model="gemini-2.5-flash", 
#     #     contents=contents,
#     #     config=config
#     # )
#     resp=None
#     try:
#         print("**** contents to LLM ****",contents)
#         resp = client.models.generate_content(
#             model="gemini-2.5-flash-lite",
#             contents=contents,
#             config=config
#         )
#         # resp =  smart_llm_call(contents, config)


#     except Exception as e:
#         error_msg = str(e)
#         if "503" in error_msg or "overloaded" in error_msg or "UNAVAILABLE" in error_msg:

#             print(" Gemini 2.5 overloaded, switching to backup model...")
#             try:
                
#                 resp = client.models.generate_content(
#                     model="gemini-2.5-flash",
#                     contents=contents,
#                     config=config
#                 )
#             except Exception as e2:
#                 print(" Backup model also failed:", e2)
#                 return "System temporarily overloaded. Please try again later."
#         else:
#             print(" Other error:", e)
#             return "Something went wrong while processing your request."
#     llm_detection_time = time.time() - llm_detection_start
#     print(f"=> LLM Function Detection Time: {llm_detection_time:.2f}s")

#     # Check if response has candidates and content
#     if not resp.candidates or not resp.candidates[0].content:
#         total_time = time.time() - total_start_time
#         print(f"❌ No response content from LLM after {total_time:.2f}s")
#         return "I apologize, but I couldn't process your request. Please try again."

#     function_calls = []
#     direct_response = None

#     for part in resp.candidates[0].content.parts:
#         if hasattr(part, 'function_call') and part.function_call:
#             function_calls.append(part.function_call)
#         elif hasattr(part, 'text') and part.text:
#             direct_response = part.text

#     if not function_calls and direct_response:
#         total_time = time.time() - total_start_time
#         print(f"=> LLM Direct Response Total Time: {total_time:.2f}s")
#         return direct_response

#     if len(function_calls) > 10:
#         print(f"  Function call limit exceeded: {len(function_calls)} > 10, truncating to first 10")
#         function_calls = function_calls[:10]
#         print(f" Remaining function calls: {[fc.name for fc in function_calls]}")

#         for fc in function_calls:
#             # Auto-fill lead_id for get_lead
#             if fc.name == "get_lead" and (not hasattr(fc, 'args') or 'lead_id' not in fc.args or not fc.args.get('lead_id')):
#                 if lead_id:
#                     if not hasattr(fc, 'args') or fc.args is None:
#                         fc.args = {}
#                     fc.args['lead_id'] = lead_id
#                     print(f"Auto-filled lead_id: {lead_id} for get_lead function")

#             elif fc.name not in ["get_lead", "get_faq_policy_search", "sales_executive_lead"] and (
#                 not hasattr(fc, 'args') or 'booking_id' not in fc.args or not fc.args.get('booking_id')
#             ):
#                 if booking_id:
#                     if not hasattr(fc, 'args') or fc.args is None:
#                         fc.args = {}
#                     fc.args['booking_id'] = booking_id
#                     print(f"Auto-filled booking_id: {booking_id} for {fc.name} function")

#             elif fc.name == "sales_executive_lead":
#                 if not hasattr(fc, 'args') or 'executive_name' not in fc.args or not fc.args.get('executive_name'):
#                     # If LLM did not provide a name, ask user
#                     missing_ids = ["executive_name"]

#         # Check for missing required parameters
#         missing_ids = []
#         for fc in function_calls:
#             if fc.name == "get_lead" and (not hasattr(fc, 'args') or 'lead_id' not in fc.args or not fc.args.get('lead_id')):
#                 missing_ids.append("lead_id")
#             elif fc.name not in ["get_lead", "get_faq_policy_search", "sales_executive_lead"] and (
#                 not hasattr(fc, 'args') or 'booking_id' not in fc.args or not fc.args.get('booking_id')
#             ):
#                 missing_ids.append("booking_id")
#             elif fc.name == "get_faq_policy_search" and (not hasattr(fc, 'args') or 'query' not in fc.args or not fc.args.get('query')):
#                 missing_ids.append("query for FAQ search")
#             elif fc.name == "sales_executive_lead" and (not hasattr(fc, 'args') or 'executive_name' not in fc.args or not fc.args.get('executive_name')):
#                 missing_ids.append("executive_name")

#         if missing_ids:
#             missing_str = " and ".join(set(missing_ids))
#             total_time = time.time() - total_start_time
#             print(f" Missing IDs Response Time: {total_time:.2f}s")
#             return f"I need {missing_str} to fetch the details. Please provide {missing_str}."


#     async def execute_function_call(fc):

#         func_start_time = time.time()
#         try:
#             print(f"=> Executing {fc.name} with args: {fc.args}")
            
#             args = dict(fc.args) if hasattr(fc, 'args') and fc.args else {}
#             if fc.name == "get_faq_policy_search" and "booking_id" in args:
#                 print(f"  Removing booking_id from FAQ search arguments")
#                 del args["booking_id"]
    
#             if fc.name == "sales_executive_lead":
#                 if not hasattr(fc, 'args') or not isinstance(fc.args, dict):
#                     fc.args = {}
#                 args = dict(fc.args)
        
#                 print(f"✅ Using sales executive as provided by LLM: {args.get('query')}")
                                    
           
#             if fc.name == "get_invoice":
#                 result = await get_invoice(**args)
#             elif fc.name == "get_booking":
#                 result = await get_booking(**args)
#             elif fc.name == "get_communication":
#                 result = await get_communication(**args)
#             elif fc.name == "get_call":
#                 result = await get_call(**args)
#             elif fc.name == "get_ticket":
#                 result = await get_ticket(**args)
#             elif fc.name == "get_lead": 
#                 result = await get_lead(**args)
#             elif fc.name == "get_faq_policy_search":
#                 result = await get_faq_policy_search(**args)
#             elif fc.name=="sales_executive_lead":

#                 result =await sales_executive_lead(**args)
#             elif fc.name=="query_executer":
#                 result=await query_executer(**args)

#             elif fc.name=="sales_team_24hr":
#                 result=await sales_team_24hr()

#             else:
#                 result = {"error": f"Unknown function {fc.name}"}
                
#             func_time = time.time() - func_start_time
#             print(f" {fc.name} executed in {func_time:.2f}s")
#             return result
            
#         except Exception as e:
#             func_time = time.time() - func_start_time
#             print(f"❌ {fc.name} failed after {func_time:.2f}s: {str(e)}")
#             return {"error": f"Function {fc.name} failed: {str(e)}"}

#     parallel_start = time.time()
#     tasks = [execute_function_call(fc) for fc in function_calls]
#     function_results = await asyncio.gather(*tasks, return_exceptions=True)
#     parallel_time = time.time() - parallel_start
#     print(f"=> All functions parallel execution time: {parallel_time:.2f}s")
                 
#     function_responses_content = [
#         types.Content(role="user", parts=[types.Part(text=query)])
#     ]

#     for fc, result in zip(function_calls, function_results):
#         function_responses_content.extend([
#             types.Content(role="model", parts=[types.Part(function_call=fc)]),
#            types.Content(
#             role="function",
#             parts=[types.Part(function_response=types.FunctionResponse(
#                 name=fc.name,
#                 response=result
#             ))]
#         )

#         ])

#     final_config = types.GenerateContentConfig(temperature=0.1)
#     print("function_responses_content:", function_responses_content)
#     try:
#         llm_response_start = time.time()
#         final_resp = client.models.generate_content(
#             model="gemini-2.5-flash-lite",
#             contents=function_responses_content,
#             config=final_config
#         )
#         # final_resp = smart_llm_call (function_responses_content, final_config)

#         llm_response_time = time.time() - llm_response_start
#         print(f" ==> Final LLM Response Generation Time: {llm_response_time:.2f}s")

#         print("Function calls:", [fc.name for fc in function_calls])
#         print("Function results:", function_results)
        
#         if (final_resp and final_resp.candidates and 
#             final_resp.candidates[0].content and 
#             final_resp.candidates[0].content.parts):
            
#             for part in final_resp.candidates[0].content.parts:
#                 if hasattr(part, 'text') and part.text:
#                     total_time = time.time() - total_start_time
#                     print(f" TOTAL LLM Function Call Time: {total_time:.2f}s")
#                     return {"function_result":function_results, "final_response": part.text}
#         else:
#             print("⚠️ No valid response from final LLM, using fallback")
#             return create_fallback_response(function_calls, function_results)
                
#     except Exception as e:
#         llm_response_time = time.time() - llm_response_start
#         print(f"❌ Final LLM failed after {llm_response_time:.2f}s: {e}")
#         total_time = time.time() - total_start_time
#         print(f" TOTAL LLM Function Call Time (with error): {total_time:.2f}s")
#         return create_fallback_response(function_calls, function_results)

#     total_time = time.time() - total_start_time
#     print(f"  TOTAL LLM Function Call Time (no response): {total_time:.2f}s")
#     return create_fallback_response(function_calls, function_results)

# def create_fallback_response(function_calls: List, function_results: List) -> str:
#     """Create a simple response when LLM fails to generate one"""
#     responses = []
#     for fc, result in zip(function_calls, function_results):
#         if not isinstance(result, Exception) and result:
#             responses.append(f"{fc.name.replace('_', ' ').title()}: {str(result)}")
    
#     if responses:
#         return "Here's the information I found:\n" + "\n".join(responses)
#     else:
#         return "I tried to fetch the information but didn't get any results."


# from google.api_core.exceptions import GoogleAPICallError

# def grpc_to_http(code):
#     mapping = {
#         14: 503, 
#         8:  429,  
#         4:  408, 
#     }
#     return mapping.get(code, 500)


# def smart_llm_call(contents, config):  
#     PRIMARY = "gemini-2.5-flash-lite"
#     BACKUP  = "gemini-2.5-flash"
#     SAFE    = "gemini-2.0-flash"

#     # Try primary
#     resp = safe_generate(PRIMARY, contents, config)  # Remove await
#     if resp:
#         return resp

#     print("⚠ Primary unavailable → switching to BACKUP")
#     resp = safe_generate(BACKUP, contents, config)  # Remove await
#     if resp:
#         return resp

#     print("⚠ Backup also failed → switching to SAFE model")
#     resp = safe_generate(SAFE, contents, config)  # Remove await
#     if resp:
#         return resp

#     print("❌ All models failed")
#     return None

# def safe_generate(model, contents, config, max_retries=5):  # Remove async
#     wait = 0.2

#     for attempt in range(max_retries):
#         try:
#             return client.models.generate_content(  
#                 model=model, contents=contents, config=config
#             )
#         except GoogleAPICallError as e:
#             http = grpc_to_http(e.code.value)
#             print(f"Retry {attempt+1}/{max_retries} → HTTP {http}")
#             if http in [503, 429, 408, 500]:
#                 time.sleep(wait)  
#                 wait = min(wait * 2, 4)
#                 continue
#             else:
#                 raise e
#     return None