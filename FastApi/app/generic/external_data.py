# # app/services/external_api.py
# import requests
# import asyncio
# import httpx
# # import httpx
# import json
# import os, sys
# import time
# # Add project root (FastApi) to sys.path
# sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

# from app.db.database import SessionLocal
# from app.model.message import Message
# from app.model.audio_file_model import AudioFile
# from sqlalchemy import or_
# from app.model.lead_history import LeadHistory

# httpx.get("https://generativelanguage.googleapis.com")




# async def fetch_booking_data(booking_id: str):
#     url = "https://www.rentmystay.com/A2/bookingDetails"
#     params = {"booking_id": booking_id}
#     headers = {"Authorization": "687ba37d3241d"}
#     resp = requests.get(url, params=params, headers=headers)
#     if resp.status_code != 200:
#         return None
#     try:
#         data = resp.json()
#     except:
#         return None
#     if not data or data.get("msg") != "Success":
#         return None

#     booking = data.get("data", {})
#     kyc_list = booking.get("kyc", [])
#     first_kyc = kyc_list[0] if kyc_list else {}

#     tenant_info = {
#         "tenant_name": first_kyc.get("tenant_name"),
#         "tenant_email": first_kyc.get("tenant_email"),
#         "tenant_phone": first_kyc.get("contact_number"),
#         "tenant_pan": first_kyc.get("pan_num"),
#     }

#     filtered_data = {
#         "booking_id": booking.get("booking_id"),
#         "user_id": booking.get("user_id"),
#         "traveller_name": booking.get("traveller_name"),
#         "contact_email": booking.get("contact_email"),
#         "booking_status": booking.get("booking_status"),
#         "property_title": booking.get("title"),
#         "tenant": {k: v for k, v in tenant_info.items() if v}
#     }
#     return filtered_data


# async def invoice_data(booking_id: str):
#     url = "https://www.rentmystay.com/A2/invoice_details"
#     params = {"booking_id": booking_id}
#     headers = {"Authorization": "687ba37d3241d"}
#     resp = requests.get(url, params=params, headers=headers)
#     if resp.status_code != 200:
#         return None
#     try:
#         full_data = resp.json()
#         data = full_data.get("data", {})
#         return {
#             "booking_id": booking_id,
#             "communications": data.get("user_communications", []),
#             "audit": data.get("audit_history", []),
#             "lead_info": data.get("booking_leads", []),
#             "checkout_instruction": data.get("checkout_instruction", [])
#         }
#     except:
#         return None

    
# async def t_booking_details_invoice(booking_id: str):
#     """Fetch both booking and invoice details and return in one dict."""
#     booking_data = await bookingss(booking_id)
#     invoice_data = await invoicess(booking_id)


#     if booking_data is None and invoice_data is None:
#         return None

#     return {
#         "booking": booking_data,
#         "invoices": invoice_data
#     }

# HEADERS = {"Authorization": "demo_token_123"}
# timeout = httpx.Timeout(10.0)  # adjust as needed

# async def fetch_json(client: httpx.AsyncClient, url: str):
#     try:
#         resp = await client.get(url, headers=HEADERS)
#         resp.raise_for_status()
#         data = resp.json()
#         if not data or data.get("msg") != "Success":
#             return None

#         results_data = data.get("data", {})
        
     
#         if "results" in results_data and isinstance(results_data["results"], dict):
#             return results_data["results"].get("results", [])
   
#         elif isinstance(results_data, list):
#             return results_data
     
#         else:
#             return results_data

#     except httpx.RequestError as e:
#         print(f"Request error for {url}: {e}")
#         return None
#     except Exception as e:
#         print(f"Other error for {url}: {e}")
#         return None

# import requests
# import asyncio
# import httpx
# import json
# import os, sys
# import time
# from app.services.cache_manager import cache_manager

# sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

# from app.db.database import SessionLocal
# from app.model.message import Message

# httpx.get("https://generativelanguage.googleapis.com")

# HEADERS = {"Authorization": "demo_token_123"}

# def remove_empty(obj):
#     if isinstance(obj, dict):
#         return {k: remove_empty(v) for k, v in obj.items() if v not in (None, "", "null")}
#     elif isinstance(obj, list):
#         return [remove_empty(v) for v in obj if v not in (None, "", "null")]
#     else:
#         return obj

# # Cached versions of your functions
# async def get_booking(booking_id: str):
#     # Try cache first
#     cached = await cache_manager.get_cached_data(booking_id, "booking")
#     if cached:
#         return cached
    
#     # Fetch from API
#     url = f"https://www.rentmystay.com/T/bookings/{booking_id}"
#     async with httpx.AsyncClient() as client:
#         resp = await client.get(url, headers=HEADERS)
#         if resp.status_code != 200:
#             return None
#         response = remove_empty(resp.json())
#         result = {"booking_data": response}
        
#         # Cache the result
#         await cache_manager.set_cached_data(booking_id, "booking", result)
#         return result

# async def get_invoice(booking_id: str):
#     cached = await cache_manager.get_cached_data(booking_id, "invoice")
#     if cached:
#         return cached
    
#     url = f"https://www.rentmystay.com/T/invoices/{booking_id}"
#     def fetch():
#         return requests.get(url, headers=HEADERS)
#     resp = await asyncio.to_thread(fetch)
#     if resp.status_code != 200:
#         return None
#     response = remove_empty(resp.json())
#     result = {"invoice_data": response}
    
#     await cache_manager.set_cached_data(booking_id, "invoice", result)
#     return result

# async def get_communication(booking_id: str):
#     cached = await cache_manager.get_cached_data(booking_id, "communication")
#     if cached:
#         return cached
    
#     url = f"https://www.rentmystay.com/T/user_communications/{booking_id}"
#     def fetch():
#         return requests.get(url, headers=HEADERS)
#     resp = await asyncio.to_thread(fetch)
#     if resp.status_code != 200:
#         return None
#     response = remove_empty(resp.json())
#     result = {"communication": response}
    
#     # Cache and categorize emails
#     await cache_manager.set_cached_data(booking_id, "communication", result)
#     await cache_manager.cache_emails_with_categories(booking_id, result)
    
#     return result

# async def get_ticket(booking_id: str):
#     cached = await cache_manager.get_cached_data(booking_id, "ticket")
#     if cached:
#         return cached
    
#     url = f"https://www.rentmystay.com/T/tickets/{booking_id}"
#     def fetch():
#         return requests.get(url, headers=HEADERS)
#     resp = await asyncio.to_thread(fetch)
#     if resp.status_code != 200:
#         return None
#     response = remove_empty(resp.json())
#     result = response
    
#     await cache_manager.set_cached_data(booking_id, "ticket", result)
#     return {"ticket":result}

# async def get_call(booking_id: str):
#     cached = await cache_manager.get_cached_data(booking_id, "call")
#     if cached:
#         return cached
    
#     url = f"https://www.rentmystay.com/T/calls/{booking_id}/10"
#     def fetch():
#         return requests.get(url, headers=HEADERS)
#     resp = await asyncio.to_thread(fetch)
#     if resp.status_code != 200:
#         return None
#     response = remove_empty(resp.json())
#     result = {"call": response}
    
#     await cache_manager.set_cached_data(booking_id, "call", result)
#     return result

# from app.model.call_log import CallLog
# from app.generic.generic_utils import api_fetcher
# from app.generic.generic_utils import whatsapp_msg_formatter, call_logs_db, call_log_api
# from datetime import datetime
# async def get_lead(lead_id: str):
#     started_time = time.time()

#     url = f"https://www.rentmystay.com/T/lead_all_data/{lead_id}"
#     print("starting lead API call")
#     api_response = await api_fetcher(url)

#     if not api_response or not api_response.get("data"):
#         print(f"lead data: {api_response}")
#         return None

#     details = api_response.get("data", {}).get("lead_details", [])
#     followups = api_response.get("data", {}).get("lead_followups", [])

#     content_parts = []
#     phone = None

   
#     if details and isinstance(details, list):
#         lead_info = details[0]
#         lead_summary = (
#             f"leadid:{lead_id}:customer_no:{lead_info.get('contact_details', '')} "
#             f"loc:{lead_info.get('location', '')} "
#             f"status:{lead_info.get('status', '')} "
#             f"sales_team:{lead_info.get('assign_to', '')}"
#         )
#         content_parts.append(lead_summary)

#         crm = "\ncrm format\ndate:sale_team: additional_info: status"
#         content_parts.append(crm)
#         for followup in followups:
#             updated_by = followup.get("added_by", "")
#             if updated_by != "System":
#                 update_time = followup.get("update_time", "")
#                 date_time=datetime.strptime(update_time, "%Y-%m-%d %H:%M:%S") if update_time else ""
#                 followup_text = (
#                     f"{date_time} {followup.get('added_by', '')}: "
#                     f"{followup.get('additional_info', '')}, {followup.get('status', '')}"
#                 )
#                 content_parts.append(followup_text)

#         phone = lead_info.get("contact_details")
#         print(f"Found phone: {phone}")

    
#     print("phone", phone)
#     if phone:
#         try:
#             whatsapp_content_parts = await whatsapp_msg_formatter(phone)
#             content_parts.extend(whatsapp_content_parts)

#             call_logs = await call_logs_db(phone)
#             call_log_format = call_logs.get("format_content")
#             print(f"call_log_format: {call_log_format}")
#             if call_log_format:
#                 content_parts.extend(call_log_format)
#             else:
                
#                 print(f"No DB logs found, fetching from API for phone: {phone}")
#                 try:
#                     call_api_data = await call_log_api(phone)
#                     if call_api_data and isinstance(call_api_data, dict):
#                         call_log_format = call_api_data.get("format_content", [])
#                     else:
#                         call_log_format = []
#                     if call_log_format:
#                         content_parts.extend(call_log_format)
#                 except Exception as api_error:
#                     print(f"API call error: {api_error}")
#                     content_parts.append(
#                         f"Sales Call Logs: Error fetching from external API ({str(api_error)})"
#                     )
#         except Exception as e:
#             print(f"Error fetching call logs: {e}")
#             content_parts.append(f"Sales Call Logs: Error fetching logs ({str(e)})")

    
#     try:
#         db = SessionLocal()
#         browsing_history = (
#             db.query(LeadHistory)
#             .filter(LeadHistory.lead_id == lead_id)
#             .order_by(LeadHistory.timestamp.asc())
#             .all()
#         )
#         browsing_history_data = []

#         if browsing_history:
#             for record in browsing_history:
#                 time_str = (
#                     record.timestamp.strftime("%Y-%m-%d %H:%M:%S")
#                     if record.timestamp
#                     else ""
#                 )
#                 browsing_history_data.append(
#                     {
#                         "timestamp": time_str,
#                         "current_page": record.current_page or "N/A",
#                         "session_id": getattr(record, "session_id", None),
#                         "source": getattr(record, "source", None),
#                         "user_agent": getattr(record, "user_agent", None),
#                         "area": getattr(record, "area", None),
#                         "name": getattr(record, "name", None),
#                         "building_name": getattr(record, "building_name", None),
#                         "furnishing_type": getattr(record, "furnishing_type", None),
#                         "unit_type": getattr(record, "unit_type", None),
#                     }
#                 )
#         else:
#             uri = f"https://www.rentmystay.com/T/customer_browsing_history_bylead/{lead_id}"
#             res_json = await api_fetcher(uri)
#             if (
#                 isinstance(res_json, dict)
#                 and res_json.get("msg") == "Success"
#                 and "data" in res_json
#                 and "lastVisited_details_of_lead" in res_json["data"]
#             ):
#                 browsing_history_data = res_json["data"]["lastVisited_details_of_lead"]
#             else:
#                 print(f"Unexpected API response structure for lead {lead_id}")
#                 print(f"Response: {res_json}")

#         if browsing_history_data:
#             content_parts.append("\nBrowsing History:")
#             for record in browsing_history_data:
#     # set defaults
#                 area = name = building_name = furnishing_type = unit_type = "N/A"

#                 if isinstance(record, dict):
#                     time_str = record.get("timestamp", "")
#                     url = record.get("current_page", "N/A")
#                     area = record.get("area", "N/A")
#                     name = record.get("name", "N/A")
#                     building_name = record.get("building_name", "N/A")
#                     furnishing_type = record.get("furnishing_type", "N/A")
#                     unit_type = record.get("unit_type", "N/A")
#                 else:
#                     time_str = (
#                         record.timestamp.strftime("%Y-%m-%d %H:%M:%S")
#                         if record.timestamp
#                         else ""
#                     )
#                     url = record.current_page or "N/A"
#                     area = record.area or "N/A"
#                     name = record.name or "N/A"
#                     building_name = record.building_name or "N/A"
#                     furnishing_type = record.furnishing_type or "N/A"
#                     unit_type = record.unit_type or "N/A"

#                 log_entry = f"{time_str} |loc:{area} |name:{name} |building_name:{building_name} |furnishing_type:{furnishing_type}  URL: {url}"
#                 content_parts.append(log_entry)

#                 if isinstance(record, dict):
#                     time_str = record.get("timestamp", "")
#                     url = record.get("current_page", "N/A")
#                 else:
#                     time_str = (
#                         record.timestamp.strftime("%Y-%m-%d %H:%M:%S")
#                         if record.timestamp
#                         else ""
#                     )
#                     url = record.current_page or "N/A"
#                     area=record.area or "N/A"
#                     name=record.name or "N/A"
#                     building_name=record.building_name or "N/A"
#                     furnishing_type=record.furnishing_type or "N/A"
#                     unit_type=record.unit_type or "N/A"


#                 log_entry = f"{time_str} |loc:{area} |name:{name} |building_name:{building_name} |furnishing_type:{furnishing_type} |unit_type:{unit_type} URL: {url}"
#                 content_parts.append(log_entry)
#         else:
#             content_parts.append("\nBrowsing history: None found")

#     except Exception as e:
#         print(f"Error in browsing history block: {e}")
#         content_parts.append(f"Browsing History: Error fetching ({e})")

#     finally:
#         try:
#             db.close()
#         except Exception:
#             pass

 
#     final_output = "\n".join(content_parts)
#     cleaned_output = "\n".join(line for line in final_output.split("\n") if line.strip())
#     print(f"🤷‍♀️ ==> lead function took: {time.time() - started_time:.2f} seconds")
#     print("lead_details:", final_output)

#     return {"data": cleaned_output}


# if __name__ == "__main__":
#      asyncio.run(get_lead("371834"))
# async def get_faq_policy_search(query: str, top_k: int = 2):
#     """
#     Perform FAQ vector similarity search using pgvector.
#     Returns only question + answer.
#     """
#     from sqlalchemy import select
#     from app.db.database import SessionLocal
#     from app.generic.embedding import generate_embedding
#     from app.model.faq_model import FAQ
    
#     db = SessionLocal()
#     try:
#         query_embedding = generate_embedding(query)

#         stmt = (
#             select(FAQ.question, FAQ.answer)
#             .order_by(FAQ.faq_vector.cosine_distance(query_embedding))
#             .limit(top_k)
#         )
#         result = db.execute(stmt)
#         faqs = result.all()

#         # Convert to list of dicts and wrap in a dictionary
#         faq_list = [{"question": q, "answer": a} for q, a in faqs]
        
#         # Return as dictionary instead of list
#         return {
#             "faqs": faq_list,
#             "count": len(faq_list),
#             "query": query
#         }
#     except Exception as e:
#         return {"error": f"FAQ search failed: {str(e)}"}
#     finally:
#         db.close()

# # import httpx

# async def scrap_data():
#     url = "https://www.rentmystay.com/T/bookings/all?limit=100&offset=0"
#     async with httpx.AsyncClient() as client:
#         resp = await client.get(url, headers=HEADERS)
#         print(f"Scrape API response data: {resp}")
#         if resp.status_code != 200:
#             return None
#         return resp.json()

# async def t_all(limit=20):
#     """Fetch all bookings with their related data"""
#     async with httpx.AsyncClient(timeout=timeout) as client:
#         # Get the list of bookings
#         booking_data = await t_allbooking(client, limit)
#         print(f"Raw booking data: {booking_data}")  # Debug
        
#         if not booking_data:
#             print("No booking data found")
#             return []

#         async def fetch_details(booking):
#             """Fetch all related data for a single booking"""
#             booking_id = booking.get("booking_id")
#             if not booking_id:
#                 print(f"No booking_id found in: {booking}")
#                 return None
            
#             print(f"Processing booking ID: {booking_id}")
            
#             try:
#                 # Fetch all related data concurrently
#                 invoice, communication, tickets, calls = await asyncio.gather(
#                     t_invoice(client, booking_id),
#                     t_communication(client, booking_id),
#                     t_ticket(client, booking_id),
#                     t_call(client, booking_id),
#                     return_exceptions=True  
#                 )
                
#                 if isinstance(invoice, Exception): invoice = []
#                 if isinstance(communication, Exception): communication = []
#                 if isinstance(tickets, Exception): tickets = []
#                 if isinstance(calls, Exception): calls = []
                
#                 result = {
#                     "booking": booking or [],  
#                     "invoice": invoice or [],
#                     "communication": communication or [],
#                     "tickets": tickets or [],
#                     "calls": calls or [],
#                 }
                
#                 print(f"✅ Successfully processed booking {booking_id}")
#                 return result
                
#             except Exception as e:
#                 print(f"❌ Error processing booking {booking_id}: {e}")
#                 return None

 
#         tasks = [fetch_details(booking) for booking in booking_data]
#         results = await asyncio.gather(*tasks)
        
 
#         valid_results = [r for r in results if r is not None]
#         print(f"✅ Completed processing {len(valid_results)} bookings")
        
#         return valid_results

# async def t_all_simple(limit=20):
#     """Simplified version for debugging"""
#     async with httpx.AsyncClient(timeout=timeout) as client:
#         booking_data = await t_allbooking(client, limit=limit)
        
#         if not booking_data:
#             return []
        
#         results = []
#         for booking in booking_data:
#             booking_id = booking.get("booking_id")
#             if booking_id:
              
#                 invoice = await t_invoice(client, booking_id)
                
#                 results.append({
#                     "booking": booking,
#                     "invoice": invoice or [],
#                     "communication": [],
#                     "tickets": [],
#                     "calls": [],
#                 })
        
#         return results

# async def t_allbooking(client, limit):
#     return await fetch_json(client, "https://www.rentmystay.com/T/bookings/all?limit=100&offset=100")
# async def t_booking(clietn, booking_id: str):
#     """Fetch specific booking details"""
#     return await fetch_json(client, f"https://www.rentmystay.com/T/bookings/{booking_id}")

# async def t_invoice(client, booking_id: str):
#     """Fetch invoices for a booking - returns list of invoices"""
#     data = await fetch_json(client, f"https://www.rentmystay.com/T/invoices/{booking_id}")
#     return data if isinstance(data, list) else []

# async def t_communication(client, booking_id: str):
#     """Fetch communications for a booking"""
#     data = await fetch_json(client, f"https://www.rentmystay.com/T/user_communications/{booking_id}")
#     return data if isinstance(data, list) else []

# async def t_ticket(client, booking_id: str):
#     """Fetch tickets for a booking"""
#     data = await fetch_json(client, f"https://www.rentmystay.com/T/tickets/{booking_id}")
#     return data if isinstance(data, list) else []

# async def t_call(client, booking_id: str):
#     """Fetch calls for a booking"""
#     data = await fetch_json(client, f"https://www.rentmystay.com/T/calls/{booking_id}/5")
#     return data if isinstance(data, list) else []


# # sync function
# # async def fetch_booking_data(booking_id: str):
# #     url = "https://www.rentmystay.com/A2/bookingDetails"
# #     params = {"booking_id": booking_id}
# #     headers = {"Authorization": "687ba37d3241d"}
# #     resp = requests.get(url, params=params, headers=headers)
# #     if resp.status_code != 200:
# #         return None
# #     try:
# #         data = resp.json()
# #     except:
# #         return None
# #     if not data or data.get("msg") != "Success":
# #         return None

# #     booking = data.get("data", {})
# #     kyc_list = booking.get("kyc", [])
# #     first_kyc = kyc_list[0] if kyc_list else {}

# #     tenant_info = {
# #         "tenant_name": first_kyc.get("tenant_name"),
# #         "tenant_email": first_kyc.get("tenant_email"),
# #         "tenant_phone": first_kyc.get("contact_number"),
# #         "tenant_pan": first_kyc.get("pan_num"),
# #     }

# #     filtered_data = {
# #         "booking_id": booking.get("booking_id"),
# #         "user_id": booking.get("user_id"),
# #         "traveller_name": booking.get("traveller_name"),
# #         "contact_email": booking.get("contact_email"),
# #         "booking_status": booking.get("booking_status"),
# #         "property_title": booking.get("title"),
# #         "tenant": {k: v for k, v in tenant_info.items() if v}
# #     }
# #     return filtered_data


# # async def app_booking_data():
# #     # 🔹 MOVE import here to break circular import
# #     from app.generic.data_collecter import get_booking_details

# #     url = "https://www.rentmystay.com/User/get_addedon_details/joyousjiji@gmail.com"
# #     headers = {"Authorization": "687ba37d3241d"}

# #     async with httpx.AsyncClient() as client:
# #         resp = await client.get(url, headers=headers)
# #     if resp.status_code != 200:
# #         return None

# #     data = resp.json()

# #     inserted_records = []
# #     with SessionLocal() as db:
# #         for booking_data in data:
# #             booking_id = booking_data.get("all_booking_ids")
# #             if not booking_id:
# #                 continue

# #             existing = await get_booking_details(booking_id)
# #             if not existing:
# #                 continue

# #             booking_json = existing.get("booking")
# #             emails_json = existing.get("emails")
# #             whatsapp_json = existing.get("whatsapp")
# #             call_logs_json = existing.get("call_logs")

# #             booking_text = json.dumps(booking_json, default=json_converter, ensure_ascii=False)
# #             emails_text = json.dumps(emails_json, default=json_converter, ensure_ascii=False)
# #             whatsapp_text = json.dumps(whatsapp_json, default=json_converter, ensure_ascii=False)
# #             calls_text = json.dumps(call_logs_json, default=json_converter, ensure_ascii=False)

# #             # embeddings (await if needed)
# #             booking_emb = generate_embedding(booking_text)
# #             email_emb = generate_embedding(emails_text)
# #             whatsapp_emb = generate_embedding(whatsapp_text)
# #             calls_emb = generate_embedding(calls_text)

# #             bj = booking_json or {}
# #             record = CustomerRecord(
# #                 booking_id=booking_id,
# #                 booking_json=booking_json,
# #                 emails_json=emails_json,
# #                 whatsapp_json=whatsapp_json,
# #                 call_logs_json=call_logs_json,
# #                 booking_status=bj.get("booking_status"),
# #                 primary_contact=bj.get("all_contacts", {}).get("primary"),
# #                 primary_email=bj.get("all_emails", {}).get("primary"),
# #                 prop_id=bj.get("prop_id"),
# #                 prop_name=bj.get("prop_name"),
# #                 travel_from_date=bj.get("travel_from_date"),
# #                 travel_to_date=bj.get("travel_to_date"),
# #                 updated_at=bj.get("updated_at"),
# #                 booking_vector=booking_emb,
# #                 email_vector=email_emb,
# #                 whatsapp_vector=whatsapp_emb,
# #                 calls_vector=calls_emb
# #             )

# #             db.add(record)
# #             db.commit()
# #             db.refresh(record)
# #             inserted_records.append(record)

# #     return inserted_records

# # from app.model.lead_details import LeadActivity, Base
# # from sqlalchemy.orm import Session
# # from app.db.database import SessionLocal
# # from app.generic.embedding import generate_embedding
# # import json
