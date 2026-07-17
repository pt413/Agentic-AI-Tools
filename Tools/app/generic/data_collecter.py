# app/services/booking_service.py
# from app.database import bookking_details_coll, emails_coll, messages_coll, call_logs_coll
from app.db.mongo_db import bookking_details_coll, emails_coll, messages_coll, call_logs_coll
# from app.services.external_api import fetch_booking_data, invoice_data
from app.generic.external_data import fetch_booking_data, invoice_data
# from app.utils import json_converter
from app.generic.converter import json_converter
from bson import ObjectId
import json
import tiktoken

async def get_booking_details(booking_id: str):
    booking_data = await bookking_details_coll.find_one({"booking_id": booking_id})
    if not booking_data:
        return {"error": f"No booking details found for {booking_id}"}

    # Collect emails & phones
    all_emails = []
    if "all_emails" in booking_data:
        all_emails.extend(booking_data["all_emails"].get("others", []))
        primary_email = booking_data["all_emails"].get("primary")
        if primary_email and primary_email not in all_emails:
            all_emails.append(primary_email)

    all_phones = []
    if "all_contacts" in booking_data:
        all_phones.extend(booking_data["all_contacts"].get("others", []))
        primary_phone = booking_data["all_contacts"].get("primary")
        if primary_phone and primary_phone not in all_phones:
            all_phones.append(primary_phone)

    # Fetch related docs
    emails_data = await emails_coll.find(
        {"$or": [{"sender": {"$in": all_emails}}, {"receiver": {"$in": all_emails}}]},
        {"embedding": 0}
    ).to_list(None)

    whatsapp_data = []
    call_logs = []
    for phone in all_phones:
        msgs = await messages_coll.find(
            {"cx_number": phone},
            {"clean_content": 0}
        ).to_list(None)
        whatsapp_data.extend(msgs)

        calls = await call_logs_coll.find({"phNum": phone[-10:]}).to_list(None)
        call_logs.extend(calls)

    # External API calls
    booking_details_from_api = await fetch_booking_data(booking_id)
    invoice_detail = await invoice_data(booking_id)

    # Core query dict
    query = {
        "booking": booking_data,
        "emails": emails_data,
        "whatsapp": whatsapp_data,
        "call_logs": call_logs,
        "booking_details": booking_details_from_api,
        "invoice_details": invoice_detail
    }

    # Token counts
    # query_str = json.dumps(query, ensure_ascii=False, default=json_converter)
    # encoding = tiktoken.encoding_for_model("gpt-4")
    # tokens = encoding.encode(query_str)

    # emails_token = len(encoding.encode(json.dumps(emails_data, default=json_converter)))
    # whatsapp_token = len(encoding.encode(json.dumps(whatsapp_data, default=json_converter)))
    # booking_token = len(encoding.encode(json.dumps(booking_details_from_api, default=json_converter)))
    # invoice_token = len(encoding.encode(json.dumps(invoice_detail, default=json_converter)))

    # query["meta"] = {
    #     "emails_token": emails_token,
    #     "whatsapp_token": whatsapp_token,
    #     "booking_token": booking_token,
    #     "invoice_token": invoice_token,
    #     "total_tokens": len(tokens),
    #     "query_string_length": len(query_str)
    # }
    print("customer:", query)
    return json.loads(json.dumps(query, default=json_converter))
