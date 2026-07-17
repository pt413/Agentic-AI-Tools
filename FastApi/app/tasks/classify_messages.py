import json
import math
import time
import re
from typing import List, Dict, Any
from datetime import datetime

from celery import shared_task, current_task
from google import genai
from google.genai import types as gen_types

from app.db.database import SessionLocal
#from app.model.message import Message
from app.model.whatsapp_chats import WhatsappChatSession


GENAI_API_KEY = "AIzaSyDt0QUFiINTDvjCY5wX6bsz2BmViWmYrkU"
MODEL_NAME = "models/gemini-2.5-flash"


DB_FETCH_LIMIT = 5

WORKER_CHUNK_SIZE = 200 

LLM_SUB_BATCH = 30


MAX_LLM_RETRIES = 4
BASE_BACKOFF = 1.0


client = genai.Client(api_key=GENAI_API_KEY)

# BATCH_PROMPT_TEMPLATE = """
# You are a strict JSON-output classifier. Given the list of input messages, return ONLY a JSON array of objects (same order) where each object has keys:
# - id: the input id
# - intent: short label (e.g., support, sales, churn_risk, feedback, greeting, other)
# - emotion: list up to 3 strings (joy, sadness, anger, fear, surprise, disgust, neutral)
# - tone: list up to 3 strings (formal, informal, urgent, frustrated, polite, sarcastic)
# - topic: short string topic (billing, technical_issue, account, feature_request, delivery, unknown)
# - actionable_signal: one of (escalate_to_support, offer_refund, ask_for_more_info, schedule_call, follow_up, none)
# - context: 1-2 sentence summary
# - outcome: expected outcome (issue_resolved, churn_prevented, sale, info_given, none)
# - language: ISO 639-1 code (en, hi, etc.)
# - If something cannot be determined, use "unknown"

# Return only valid JSON array (no explanation). Keep response compact. Example:
# Input: [{"id":"m1","text":"I want to cancel my subscription"}, {"id":"m2","text":"Thanks, great service!"}]
# Output: [{"id":"m1","intent":"churn_risk","emotion":["anger"],"tone":["frustrated"],"topic":"subscription","actionable_signal":"escalate_to_support","context":"User upset about subscription and wants cancellation","outcome":"issue_resolved","language":"en"}, {"id":"m2","intent":"feedback","emotion":["joy"],"tone":["polite"],"topic":"praise","actionable_signal":"none","context":"User praising the service","outcome":"none","language":"en"}]

# Now classify the following input strictly into a JSON array:
# INPUT_JSON
# """
BATCH_PROMPT_TEMPLATE = r"""
You are a strict JSON-output classifier. You will be given a JSON array named INPUT_JSON (a list of objects).  
**Each input object will have these keys:**  
  - "id": string (message/session id)  
  - "text": string (message text)  
  - "direction": string, one of "incoming" or "outgoing" (incoming = customer/user message, outgoing = agent/salesperson message)  
  - "timestamp": string in ISO-8601 format "YYYY-MM-DD HH:MM" optionally followed by a space and a timezone offset like "+05:30" or "Z" (examples: "2025-11-03 08:30", "2025-11-03 08:30 +05:30", or "2025-11-03 03:00Z")

Your job: for each input object, output a single JSON object (same order) with only the keys listed below.  
**RETURN EXACTLY ONE JSON ARRAY** — no explanations, no extra text, no markdown, no backticks.

INPUT shape example (what will be provided to you):
[{"id":"m1","text":"Price is high. I am looking for 15k","direction":"incoming","timestamp":"2025-11-03 09:12 +05:30"},
 {"id":"m2","text":"Sure — we have one at 18k flexi","direction":"outgoing","timestamp":"2025-11-03 09:13 +05:30"}]

OUTPUT SCHEMA (each array element must contain these keys only, in this order):
[
  {
    "id": "<same id as input>",
    "intent": "<intent_label>",
    "emotion": ["<emotion1>", "<emotion2>", ...],        # up to 3
    "tone": ["<tone1>", "<tone2>", ...],                # up to 3
    "topic": "<topic_label>",
    "actionable_signal": "<one_of_actionable_values>",
    "context": "<1-2 sentence concise summary (no quotes inside)>",
    "outcome": "<outcome_label>",
    "language": "<ISO-639-1 code>"
  },
  ...
]

STRICT RULES:
1. **JSON only**. The assistant must output a single JSON array and nothing else.
2. **Do not add, remove or rename keys** in the OUTPUT objects. If you cannot determine a value, use the string `"unknown"` (for arrays, use `["unknown"]`).
3. **Do not output confidence scores or probabilities.**
4. **Keep arrays short**: emotion and tone — up to 3 items each.
5. **Language** must be ISO-639-1 two-letter code (e.g., "en", "hi", "bn"). If unsure, return "unknown".
6. **Context** must be 1–2 short sentences summarizing the message's main ask / objection / request / state. Keep it factual and grounded in the input text.
7. **All labels must be normalized** to the enumerated tokens below. If an appropriate label is not available, choose `"other"` (if allowed) or `"unknown"`.

TIMESTAMP & TIMEZONE GUIDANCE:
- Timestamps in INPUT_JSON will be in local time or include an explicit timezone offset. Do **not** alter or convert timestamps. Treat the timestamp string as provided.  
- If the timestamp lacks an explicit timezone, assume it is local time and **do not** convert it. (If you cannot infer language or timezone from text, return `"unknown"` for language and proceed.)
- Use timestamps only as contextual metadata — you do NOT need to compute session aggregates or trajectories here. Include only the required OUTPUT schema keys.

TAXONOMY (use these exact label tokens):

INTENT (choose one primary label)
- support
- sales_inquiry
- pricing_negotiation
- booking_request
- churn_intent
- feedback
- greeting
- info_request
- escalation_request
- cancellation
- complaint
- other
- unknown

EMOTION (pick up to 3)
- neutral
- satisfaction
- joy
- gratitude
- disappointment
- dissatisfaction
- annoyance
- frustration
- anger
- urgency
- fear
- surprise
- sadness
- relief
- other
- unknown

TONE (pick up to 3)
- neutral
- polite
- informal
- formal
- negotiating
- objecting
- dissatisfied
- frustrated
- urgent
- sarcastic
- appreciative
- apologetic
- inquisitive
- other
- unknown

TOPIC (one short label)
- billing
- pricing
- availability
- location
- amenities
- booking_process
- contract_terms
- deposit
- refund
- maintenance
- property_details
- cancellation_policy
- support_contact
- promotion
- unknown
- other

ACTIONABLE_SIGNAL (choose one)
- escalate_to_support
- schedule_visit
- offer_discount
- offer_refund
- provide_location
- ask_for_more_info
- send_listing
- confirm_booking
- follow_up
- none
- unknown

OUTCOME (one)
- issue_resolved
- sale
- scheduled_visit
- info_provided
- churn_prevented
- no_action
- follow_up_required
- unknown

DETAILED GUIDELINES / INSTRUCTIONS:
- Prioritize **exact textual evidence** from the input `text`. If the text contains explicit negotiation language (e.g., "I am looking for 15k", "Price is high", "Can you do 15k?"), mark `intent` as `pricing_negotiation` and include `tone` such as `negotiating` or `objecting`. Use `emotion` like `dissatisfaction` or `disappointment` when appropriate.
- If the user explicitly requests a visit, call, booking, or caretaker number, set `actionable_signal` to `schedule_visit` / `confirm_booking` / `provide_location` / `send_listing` accordingly.
- For polite thanks (e.g., "Thanks, great service"), use `emotion: ["gratitude"]`, `intent: feedback` or `intent: other` depending on broader context.
- If the text contains a clear complaint (words like "not working", "issue", "angry", "this is unacceptable"), use `intent: complaint` and set `actionable_signal` to `escalate_to_support` if the user asks for escalation.
- If the message mentions cancellation or wanting to stop service, set `intent: cancellation` and `actionable_signal: follow_up`.
- If the message asks factual questions ("where is this located?", "what is the rent?"), use `intent: info_request` and `actionable_signal: ask_for_more_info` if appropriate.
- If multiple intents are implied, pick the **primary** one.
- If the message contains phone numbers, caretaker details, URLs or listing links, prefer `actionable_signal: send_listing` or `provide_location` only if the user explicitly asked to share or requested location. Otherwise use `none`.
- Always keep `context` concise and factual, e.g.: "User objects to price and requests 15k." or "Agent shared 4 listings; user responded with price target 15k."

PARSE & SANITIZE RULES:
- Remove extraneous whitespace. If the text is empty or contains only URLs and no user message, set `intent: info_request` or `other` based on the URL; otherwise `unknown`.
- Do NOT invent facts. Only extract or infer minimal, reasonable information from the text.
- Do not attempt to merge or summarize multiple distinct messages into one output object — each input element should produce one output object independent of sequence. (Session-level aggregation or trajectories are out-of-scope for this prompt.)

OUTPUT EXAMPLES (must follow EXACT format):

Example 1:
Input:
[{"id":"m1","text":"Price is high. I am looking for 15k","direction":"incoming","timestamp":"2025-11-03 09:12 +05:30"}]
Output:
[{"id":"m1","intent":"pricing_negotiation","emotion":["dissatisfaction"],"tone":["negotiating","objecting"],"topic":"pricing","actionable_signal":"ask_for_more_info","context":"User objects to listed prices and requests ₹15,000 budget.","outcome":"follow_up_required","language":"en"}]

Example 2:
Input:
[{"id":"m2","text":"Thanks, great service!","direction":"incoming","timestamp":"2025-11-03 10:00 +05:30"}]
Output:
[{"id":"m2","intent":"feedback","emotion":["gratitude"],"tone":["polite"],"topic":"other","actionable_signal":"none","context":"User praises the service.","outcome":"no_action","language":"en"}]

Example 3:
Input:
[{"id":"m3","text":"Can you share the location for the Bomanalli listing?","direction":"incoming","timestamp":"2025-11-03 11:12 +05:30"}]
Output:
[{"id":"m3","intent":"info_request","emotion":["neutral"],"tone":["inquisitive"],"topic":"location","actionable_signal":"provide_location","context":"User requests location details for Bomanalli listing.","outcome":"info_provided","language":"en"}]

FINAL STEP:
Now classify the following input strictly into a JSON array (preserve the input order). Use only the schema and labels above. If you cannot determine a field, use "unknown" (or ["unknown"] for arrays). INPUT_JSON
"""


_CODEFENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", flags=re.DOTALL | re.IGNORECASE)

def _strip_codefence(text: str) -> str:
    if not text:
        return text
    m = _CODEFENCE_RE.match(text)
    if m:
        return m.group(1).strip()
    return text.strip().strip("` \n\t")

def _call_gemini_prompt(prompt: str) -> str:
    """
    Calls genai client.models.generate_content and returns the extracted text.
    Retries handled at a higher level if needed.
    """
    resp = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    
    text = getattr(resp, "text", None)
    if text:
        return _strip_codefence(text)
    return _strip_codefence(str(resp))

def _call_gemini_with_retries(prompt: str, max_retries: int = MAX_LLM_RETRIES, base_backoff: float = BASE_BACKOFF) -> str:
    attempt = 0
    while True:
        attempt += 1
        try:
            return _call_gemini_prompt(prompt)
        except Exception as exc:
            msg = str(exc)
            
            retry_delay = None
            try:
                if len(exc.args) > 0:
                    info = exc.args[0]
                    if isinstance(info, dict):
                        for d in info.get("error", {}).get("details", []) or []:
                            rd = d.get("retryDelay")
                            if rd:
                                import re
                                m = re.search(r"(\d+)", str(rd))
                                if m:
                                    retry_delay = int(m.group(1))
                                    break
            except Exception:
                retry_delay = None

            if "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower() or "429" in msg:
                wait = retry_delay or min(base_backoff * (2 ** (attempt - 1)), 60)
                current_task.update_state(state="RETRYING", meta={"attempt": attempt, "reason": "quota", "wait": wait})
                time.sleep(wait)
            elif "UNAVAILABLE" in msg or "503" in msg:
                wait = retry_delay or min(base_backoff * (2 ** (attempt - 1)), 60)
                current_task.update_state(state="RETRYING", meta={"attempt": attempt, "reason": "unavailable", "wait": wait})
                time.sleep(wait)
            else:
                
                raise

            if attempt >= max_retries:
                raise

def _build_batch_prompt(items: List[Dict[str, str]]) -> str:
    payload = json.dumps(items, ensure_ascii=False)
    return BATCH_PROMPT_TEMPLATE.replace("INPUT_JSON", payload)

def _parse_batch_response(text: str) -> List[Dict[str, Any]]:
    """
    Expect text to be a JSON array. Try to strip fences and parse.
    """
    cleaned = _strip_codefence(text)
    parsed = json.loads(cleaned)
    if not isinstance(parsed, list):
        raise ValueError("Expected JSON array")
    return parsed



def _list_to_csv(val):
    if val is None:
        return None
    if isinstance(val, str):
        return val
    if isinstance(val, (list, tuple)):
        cleaned = [str(x).strip() for x in val if x is not None and str(x).strip() != ""]
        return ", ".join(cleaned) if cleaned else None
    return str(val)


@shared_task(bind=True)
def enqueue_unclassified_messages(self, batch_total: int = DB_FETCH_LIMIT):
    """
    Fetch up to batch_total message_ids where intent IS NULL and enqueue worker tasks.
    """
    db = SessionLocal()
    try:
        rows = db.query(WhatsappChatSession).filter(WhatsappChatSession.intent == None).limit(batch_total).all()
        ids = [r.id for r in rows]
    finally:
        db.close()

    
    for i in range(0, len(ids), WORKER_CHUNK_SIZE):
        chunk = ids[i:i+WORKER_CHUNK_SIZE]
        process_chunk.delay(chunk)
    return {"enqueued": len(ids)}

@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def process_chunk(self, id_list: List[str]):
    """
    Celery worker: process up to WORKER_CHUNK_SIZE message ids.
    Within worker we call Gemini in LLM_SUB_BATCH groups (each call classifies up to LLM_SUB_BATCH messages).
    """
    if not id_list:
        return {"processed": 0}
    db = SessionLocal()
    processed = 0
    try:
        rows = db.query(WhatsappChatSession).filter(WhatsappChatSession.id.in_(id_list)).all()
        
        id_to_row = {r.id: r for r in rows}
        ordered_rows = [id_to_row[mid] for mid in id_list if mid in id_to_row]

        
        for i in range(0, len(ordered_rows), LLM_SUB_BATCH):
            sub = ordered_rows[i:i+LLM_SUB_BATCH]
            items = [{"id": r.id, "text": (r.conversation_summary or "")} for r in sub]
            prompt = _build_batch_prompt(items)

           
            try:
                resp_text = _call_gemini_with_retries(prompt)
            except Exception as exc:
                
                raise self.retry(exc=exc)

            
            try:
                classified_list = _parse_batch_response(resp_text)
            except Exception as parse_exc:
                
                raise self.retry(exc=parse_exc)

            
            for item in classified_list:
                mid = item.get("id")
                if not mid:
                    continue
                row = id_to_row.get(mid)
                if not row:
                    continue
                
                row.intent = item.get("intent")
                # changed to store as comma-separated strings for UI readability
                row.emotion = _list_to_csv(item.get("emotion")) if item.get("emotion") is not None else None
                row.tone = _list_to_csv(item.get("tone")) if item.get("tone") is not None else None
                row.topic = item.get("topic")
                row.actionable_signal = item.get("actionable_signal")
                row.context = item.get("context")
                row.outcome = item.get("outcome")
                row.language = item.get("language")
                db.add(row)
                processed += 1

            
            db.commit()
            time.sleep(0.2)  

        return {"processed": processed}
    finally:
        db.close()


# if __name__ == "__main__":
#     db = SessionLocal()
#     rows = db.query(WhatsappChatSession).filter(WhatsappChatSession.intent == None).limit(30).all()
#     ids = [r.id for r in rows]
#     print("Running locally for IDs:", ids)

#     from app.tasks.classify_messages import process_chunk
#     print(process_chunk(id_list=ids))




# import json
# import math
# import time
# import re
# from typing import List, Dict, Any
# from datetime import datetime

# from celery import shared_task, current_task
# from google import genai
# from google.genai import types as gen_types

# from app.db.database import SessionLocal
# #from app.model.message import Message
# from app.model.whatsapp_chats import WhatsappChatSession


# GENAI_API_KEY = "AIzaSyDt0QUFiINTDvjCY5wX6bsz2BmViWmYrkU"
# MODEL_NAME = "models/gemini-2.5-flash"


# DB_FETCH_LIMIT = 10

# WORKER_CHUNK_SIZE = 200 

# LLM_SUB_BATCH = 10  



# MAX_LLM_RETRIES = 4
# BASE_BACKOFF = 1.0


# client = genai.Client(api_key=GENAI_API_KEY)


# BATCH_PROMPT_TEMPLATE = r"""
# You are a strict JSON-output classifier.  You will be given a JSON array named INPUT_JSON (list of objects with keys "id" and "text").  
# Your job: for each input object, output a single JSON object (same order) with only the keys listed below.  
# **RETURN EXACTLY ONE JSON ARRAY** — no explanations, no extra text, no markdown, no backticks.

# OUTPUT SCHEMA (each array element must contain these keys only, in this order):
# [
#   {
#     "id": "<same id as input>",
#     "intent": "<intent_label>",
#     "emotion": ["<emotion1>", "<emotion2>", ...],        # up to 3
#     "tone": ["<tone1>", "<tone2>", ...],                # up to 3
#     "topic": "<topic_label>",
#     "actionable_signal": "<one_of_actionable_values>",
#     "context": "<1-2 sentence concise summary (no quotes inside)>",
#     "outcome": "<outcome_label>",
#     "language": "<ISO-639-1 code>"
#   },
#   ...
# ]

# STRICT RULES:
# 1. **JSON only**. The assistant must output a single JSON array and nothing else.
# 2. **Do not add, remove or rename keys**. If you cannot determine a value, use the string `"unknown"` (for arrays, use `["unknown"]`).
# 3. **Do not output confidence scores or probabilities.**
# 4. **Keep arrays short**: emotion and tone -- up to 3 items each.
# 5. **Language** must be ISO-639-1 two-letter code (e.g., "en", "hi", "bn"). If unsure, return "unknown".
# 6. **Context** must be 1–2 short sentences summarizing the user's main ask / objection / request / state. Keep it factual and grounded in the input text.
# 7. **All labels must be normalized** to the enumerated values below. If an appropriate label is not available, choose `"other"` (if allowed) or `"unknown"`.

# TAXONOMY (use these exact label tokens):

# INTENT (choose one primary label)
# - support
# - sales_inquiry
# - pricing_negotiation
# - booking_request
# - churn_intent
# - feedback
# - greeting
# - info_request
# - escalation_request
# - cancellation
# - complaint
# - other
# - unknown

# EMOTION (pick up to 3)
# - neutral
# - satisfaction
# - joy
# - gratitude
# - disappointment
# - dissatisfaction
# - annoyance
# - frustration
# - anger
# - urgency
# - fear
# - surprise
# - sadness
# - relief
# - other
# - unknown

# TONE (pick up to 3)
# - neutral
# - polite
# - informal
# - formal
# - negotiating
# - objecting
# - dissatisfied
# - frustrated
# - urgent
# - sarcastic
# - appreciative
# - apologetic
# - inquisitive
# - other
# - unknown

# TOPIC (one short label)
# - billing
# - pricing
# - availability
# - location
# - amenities
# - booking_process
# - contract_terms
# - deposit
# - refund
# - maintenance
# - property_details
# - cancellation_policy
# - support_contact
# - promotion
# - unknown
# - other

# ACTIONABLE_SIGNAL (choose one)
# - escalate_to_support
# - schedule_visit
# - offer_discount
# - offer_refund
# - provide_location
# - ask_for_more_info
# - send_listing
# - confirm_booking
# - follow_up
# - none
# - unknown

# OUTCOME (one)
# - issue_resolved
# - sale
# - scheduled_visit
# - info_provided
# - churn_prevented
# - no_action
# - follow_up_required
# - unknown

# DETAILED GUIDELINES / INSTRUCTIONS:
# - Prioritize **exact textual evidence** from the input. If the text contains explicit negotiation language (e.g., "I am looking for 15k", "Price is high", "Can you do 15k?"), mark `intent` as `pricing_negotiation` and `tone` should include `negotiating` or `objecting` as appropriate. Do NOT mark this as `frustration` alone — use `dissatisfaction` or `objecting` in `tone` and `dissatisfaction` or `disappointment` in `emotion` when the user is pushing back on price.
# - If the user explicitly requests a visit, call, booking, or caretaker number, set `actionable_signal` to `schedule_visit` / `confirm_booking` / `provide_location` / `send_listing` accordingly.
# - For polite thanks (e.g., "Thanks, great service"), use `emotion: ["gratitude"]`, `intent: feedback` or `intent: other` depending on context.
# - If the text contains a clear complaint (words like "not working", "issue", "angry", "this is unacceptable"), use `intent: complaint` and set `actionable_signal` to `escalate_to_support` if the user is asking for escalation.
# - If the message mentions cancellation or wanting to stop service, set `intent: cancellation` / `actionable_signal: follow_up`.
# - If user is asking factual questions ("where is this located?", "what is the rent?"), use `intent: info_request` and `actionable_signal: ask_for_more_info` if appropriate.
# - If multiple intents are implied, pick the **primary** one.
# - If the message contains a phone number, caretaker details, URLs or listing links, prefer `actionable_signal: send_listing` or `provide_location` only if the user asked to share or requested the location. Otherwise `none`.
# - Always keep `context` concise and factual, e.g.: "User objects to price and requests 15k." or "Agent shared 4 listings; user responded with price target 15k."
# - Preserve chronology not required — process each message independently (the input may be a conversation summary string).

# PARSE & SANITIZE RULES:
# - Remove extraneous whitespace. If the text is empty or contains only URLs and no user message, set `intent: info_request` or `other` based on what the URL indicates; otherwise `unknown`.
# - Do NOT invent facts. Only extract or infer minimal, reasonable information from the text.

# OUTPUT EXAMPLES (must follow EXACT format):

# Example 1:
# Input: [{"id":"m1","text":"Price is high. I am looking for 15k"}]
# Output:
# [{"id":"m1","intent":"pricing_negotiation","emotion":["dissatisfaction"],"tone":["negotiating","objecting"],"topic":"pricing","actionable_signal":"ask_for_more_info","context":"User objects to listed prices and requests ₹15,000 budget.","outcome":"follow_up_required","language":"en"}]

# Example 2:
# Input: [{"id":"m2","text":"Thanks, great service!"}]
# Output:
# [{"id":"m2","intent":"feedback","emotion":["gratitude"],"tone":["polite"],"topic":"other","actionable_signal":"none","context":"User praises the service.","outcome":"no_action","language":"en"}]

# Example 3:
# Input: [{"id":"m3","text":"Can you share the location for the Bomanalli listing?"}]
# Output:
# [{"id":"m3","intent":"info_request","emotion":["neutral"],"tone":["inquisitive"],"topic":"location","actionable_signal":"provide_location","context":"User requests location details for Bomanalli listing.","outcome":"info_provided","language":"en"}]

# FINAL STEP:
# Now classify the following input strictly into a JSON array (preserve the input order). Use only the schema and labels above. If you cannot determine a field, use "unknown" (or ["unknown"] for arrays). INPUT_JSON
# """


# _CODEFENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", flags=re.DOTALL | re.IGNORECASE)

# def _strip_codefence(text: str) -> str:
#     if not text:
#         return text
#     m = _CODEFENCE_RE.match(text)
#     if m:
#         return m.group(1).strip()
#     return text.strip().strip("` \n\t")

# def _call_gemini_prompt(prompt: str) -> str:
#     """
#     Calls genai client.models.generate_content and returns the extracted text.
#     Retries handled at a higher level if needed.
#     """
#     resp = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    
#     text = getattr(resp, "text", None)
#     if text:
#         return _strip_codefence(text)
#     return _strip_codefence(str(resp))

# def _call_gemini_with_retries(prompt: str, max_retries: int = MAX_LLM_RETRIES, base_backoff: float = BASE_BACKOFF) -> str:
#     attempt = 0
#     while True:
#         attempt += 1
#         try:
#             return _call_gemini_prompt(prompt)
#         except Exception as exc:
#             msg = str(exc)
            
#             retry_delay = None
#             try:
#                 if len(exc.args) > 0:
#                     info = exc.args[0]
#                     if isinstance(info, dict):
#                         for d in info.get("error", {}).get("details", []) or []:
#                             rd = d.get("retryDelay")
#                             if rd:
#                                 import re
#                                 m = re.search(r"(\d+)", str(rd))
#                                 if m:
#                                     retry_delay = int(m.group(1))
#                                     break
#             except Exception:
#                 retry_delay = None

#             if "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower() or "429" in msg:
#                 wait = retry_delay or min(base_backoff * (2 ** (attempt - 1)), 60)
#                 current_task.update_state(state="RETRYING", meta={"attempt": attempt, "reason": "quota", "wait": wait})
#                 time.sleep(wait)
#             elif "UNAVAILABLE" in msg or "503" in msg:
#                 wait = retry_delay or min(base_backoff * (2 ** (attempt - 1)), 60)
#                 current_task.update_state(state="RETRYING", meta={"attempt": attempt, "reason": "unavailable", "wait": wait})
#                 time.sleep(wait)
#             else:
                
#                 raise

#             if attempt >= max_retries:
#                 raise

# def _build_batch_prompt(items: List[Dict[str, str]]) -> str:
#     payload = json.dumps(items, ensure_ascii=False)
#     return BATCH_PROMPT_TEMPLATE.replace("INPUT_JSON", payload)

# def _parse_batch_response(text: str) -> List[Dict[str, Any]]:
#     """
#     Expect text to be a JSON array. Try to strip fences and parse.
#     """
#     cleaned = _strip_codefence(text)
#     parsed = json.loads(cleaned)
#     if not isinstance(parsed, list):
#         raise ValueError("Expected JSON array")
#     return parsed



# def _list_to_csv(val):
#     if val is None:
#         return None
#     if isinstance(val, str):
#         return val
#     if isinstance(val, (list, tuple)):
#         cleaned = [str(x).strip() for x in val if x is not None and str(x).strip() != ""]
#         return ", ".join(cleaned) if cleaned else None
#     return str(val)


# @shared_task(bind=True)
# def enqueue_unclassified_messages(self, batch_total: int = DB_FETCH_LIMIT):
#     """
#     Fetch up to batch_total message_ids where intent IS NULL and enqueue worker tasks.
#     """
#     db = SessionLocal()
#     try:
#         rows = db.query(WhatsappChatSession).filter(WhatsappChatSession.intent == None).limit(batch_total).all()
#         ids = [r.id for r in rows]
#     finally:
#         db.close()

    
#     for i in range(0, len(ids), WORKER_CHUNK_SIZE):
#         chunk = ids[i:i+WORKER_CHUNK_SIZE]
#         process_chunk.delay(chunk)
#     return {"enqueued": len(ids)}

# @shared_task(bind=True, max_retries=2, default_retry_delay=60)
# def process_chunk(self, id_list: List[str]):
#     """
#     Celery worker: process up to WORKER_CHUNK_SIZE message ids.
#     Within worker we call Gemini in LLM_SUB_BATCH groups (each call classifies up to LLM_SUB_BATCH messages).
#     """
#     if not id_list:
#         return {"processed": 0}
#     db = SessionLocal()
#     processed = 0
#     try:
#         # fetch DB rows for provided ids
#         rows = db.query(WhatsappChatSession).filter(WhatsappChatSession.id.in_(id_list)).all()
        
#         # === CHANGED: normalize DB keys to strings to avoid int/str mismatches ===
#         id_to_row = {str(r.id): r for r in rows}
#         ordered_rows = [id_to_row[str(mid)] for mid in id_list if str(mid) in id_to_row]

#         for i in range(0, len(ordered_rows), LLM_SUB_BATCH):
#             sub = ordered_rows[i:i+LLM_SUB_BATCH]
#             # === CHANGED: ensure item ids are strings and text fallback is a string ===
#             items = [{"id": str(r.id), "text": (r.conversation_summary or "").strip()} for r in sub]
#             prompt = _build_batch_prompt(items)

#             # debug: small prompt/info logs for local runs
#             try:
#                 print(f"[debug] Calling LLM for batch size={len(items)} prompt_len={len(prompt)}")
#             except Exception:
#                 pass

           
#             try:
#                 resp_text = _call_gemini_with_retries(prompt)
#             except Exception as exc:
                
#                 raise self.retry(exc=exc)

#             # debug: print raw response snippet
#             try:
#                 print("[debug] raw LLM resp (first 2000 chars):", (resp_text or "")[:2000])
#             except Exception:
#                 pass
            
#             try:
#                 classified_list = _parse_batch_response(resp_text)
#             except Exception as parse_exc:
#                 # debug: show raw response on parse failure to help troubleshooting
#                 print("[error] Failed to parse LLM response. Raw response:", resp_text)
#                 raise self.retry(exc=parse_exc)

            
#             for item in classified_list:
#                 mid = item.get("id")
#                 if mid is None:
#                     continue
#                 # === CHANGED: normalize returned id to string for lookup ===
#                 mid = str(mid)
#                 row = id_to_row.get(mid)
#                 if not row:
#                     print(f"[warn] parsed id {mid} not found in fetched rows (available keys sample: {list(id_to_row.keys())[:5]})")
#                     continue
                
#                 row.intent = item.get("intent")
#                 # changed to store as comma-separated strings for UI readability
#                 row.emotion = _list_to_csv(item.get("emotion")) if item.get("emotion") is not None else None
#                 row.tone = _list_to_csv(item.get("tone")) if item.get("tone") is not None else None
#                 row.topic = item.get("topic")
#                 row.actionable_signal = item.get("actionable_signal")
#                 row.context = item.get("context")
#                 row.outcome = item.get("outcome")
#                 row.language = item.get("language")
#                 db.add(row)
#                 processed += 1

            
#             db.commit()
#             time.sleep(0.2)  

#         return {"processed": processed}
#     finally:
#         db.close()


# if __name__ == "__main__":
#     db = SessionLocal()
    
#     ids = ['1989']
#     print("Running locally for IDs:", ids)

#     from app.tasks.classify_messages import process_chunk
#     print(process_chunk(id_list=ids))

