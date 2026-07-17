#openai

import os
import re
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

ALLOWED_TYPES = {
    "invoice",
    "payment_receipt",
    "id_proof",
    "visitor_log",
    "geo_tagged_property",
    "property_inventory",
    "move_in_out",
    "staff_attendance",
    "property",
    "unknown",
}


def normalize_label(label: str) -> str:
    label = (label or "").strip().lower()

    # remove punctuation
    label = re.sub(r"[^a-z0-9_ ]", "", label)

    # normalize spaces
    label = label.replace("-", "_")
    label = label.replace(" ", "_")

    # synonym normalization
    mapping = {
        "geo_tagged": "geo_tagged_property",
        "geo_tagged_image": "geo_tagged_property",
        "geo_property": "geo_tagged_property",
        "property_geo": "geo_tagged_property",

        "visitor": "visitor_log",
        "visitorlog": "visitor_log",

        "payment": "payment_receipt",
        "payment_proof": "payment_receipt",
        "receipt": "payment_receipt",

        "inventory": "property_inventory",

        "moveinout": "move_in_out",
        "movein_out": "move_in_out",

        "staff": "staff_attendance",
        "attendance": "staff_attendance",

        "property_related": "property",
        "building_property": "property",
    }

    label = mapping.get(label, label)

    return label


'''def classify_image_type_from_text(text: str) -> str:
    text = (text or "").strip().lower()

    if not text:
        return "unknown"

    # ---------- RULES FIRST ----------

    if any(x in text for x in [
        "visitor log",
        "site visit",
        "visitor contact number",
        "your site visit information",
        "select property",
        "sitevisit",
        "visitor",
        "booking"
    ]):
        return "visitor_log"

    if any(x in text for x in [
        "payment successful",
        "transaction id",
        "upi",
        "imps",
        "paytm",
        "phonepe",
        "gpay",
        "paid in",
        "sent",
        "payment mode",
        "ref no"
    ]):
        return "payment_receipt"

    if any(x in text for x in [
        "aadhaar",
        "aadhar",
        "government of india",
        "permanent account number",
        "driving licence",
        "driving license",
        "voter id",
        "passport"
    ]):
        return "id_proof"

    if any(x in text for x in [
        "move_in/out",
        "move in/out",
        "no damage",
        "damages",
        "files uploaded",
        "aadharcardfront",
        "save & create",
        "move_in",
        "move_out"
    ]):
        return "move_in_out"

    if any(x in text for x in [
        "asset",
        "asset id",
        "fridge",
        "gas_stove",
        "gas stove",
        "cot",
        "tv_32",
        "center_table",
        "category",
        "description"
    ]):
        return "property_inventory"

    if any(x in text for x in [
        "trainer name",
        "batch",
        "caretakers",
        "managers name",
        "employee",
        "staff"
    ]):
        return "staff_attendance"

    if any(x in text for x in [
        "bengaluru",
        "bangalore division",
        "karnataka",
        "kasavanahalli",
        "btm layout",
        "garvebhavi palya",
        "bommanahalli",
        "latitude",
        "longitude",
        "altitude",
        "google",
        "outer ring rd"
    ]) or re.search(r"\d+\.\d+[ns]\s*\d+\.\d+[ew]", text):
        return "geo_tagged_property"

    if any(x in text for x in [
        "invoice",
        "gst",
        "tax invoice",
        "receipt",
        "bill",
        "invoice no",
        "total amount"
    ]):
        return "invoice"

    if any(x in text for x in [
        "property",
        "tenant",
        "rent",
        "agreement",
        "room",
        "flat",
        "premises"
    ]):
        return "property"'''




def classify_image_type_from_text(text: str) -> str:
    text = (text or "").strip().lower()

    if not text:
        return "unknown"

    # =====================================================
    # GEO TAGGED PROPERTY (HIGH PRIORITY)
    # Must run BEFORE payment detection
    # =====================================================
    geo_indicators = [
        "bengaluru",
        "bangalore",
        "bangalore division",
        "karnataka",
        "kasavanahalli",
        "btm layout",
        "garvebhavi palya",
        "bommanahalli",
        "latitude",
        "longitude",
        "altitude",
        "google",
        "outer ring rd",
        "maps",
        "location",
        "speed:",
        "km/h",
        "cross rd",
        "main rd"
    ]

    geo_hits = sum(1 for x in geo_indicators if x in text)

    if geo_hits >= 2:
        return "geo_tagged_property"

    if re.search(r"\d+\.\d+\s*[ns]", text) and re.search(r"\d+\.\d+\s*[ew]", text):
        return "geo_tagged_property"

    # =====================================================
    # VISITOR LOG
    # =====================================================
    if any(x in text for x in [
        "visitor log",
        "site visit",
        "visitor contact number",
        "your site visit information",
        "select property",
        "sitevisit",
        "visitor"
    ]):
        return "visitor_log"

    # =====================================================
    # STRICT PAYMENT RECEIPT
    # Payment apps alone should NOT classify as payment
    # =====================================================
    payment_brands = [
        "paytm",
        "phonepe",
        "google pay",
        "gpay",
        "bhim",
        "amazon pay",
        "mobikwik"
    ]

    strong_payment_terms = [
        "payment successful",
        "transaction id",
        "utr",
        "rrn",
        "imps",
        "neft",
        "rtgs",
        "net banking",
        "paid to",
        "received from",
        "debited from",
        "credited to",
        "payment mode",
        "ref no",
        "completed"
    ]

    weak_payment_terms = [
        "upi",
        "upi id",
        "sent"
    ]

    brand_hit = any(x in text for x in payment_brands)
    strong_hits = sum(1 for x in strong_payment_terms if x in text)
    weak_hits = sum(1 for x in weak_payment_terms if x in text)

    # Valid payment receipt logic
    if strong_hits >= 2:
        return "payment_receipt"

    if brand_hit and strong_hits >= 1:
        return "payment_receipt"

    if weak_hits >= 2 and strong_hits >= 1:
        return "payment_receipt"

    # =====================================================
    # ID PROOF
    # =====================================================
    if any(x in text for x in [
        "aadhaar",
        "aadhar",
        "government of india",
        "permanent account number",
        "pan card",
        "driving licence",
        "driving license",
        "voter id",
        "passport"
    ]):
        return "id_proof"

    # =====================================================
    # MOVE IN / OUT
    # =====================================================
    if any(x in text for x in [
        "move_in/out",
        "move in/out",
        "no damage",
        "damages",
        "files uploaded",
        "aadharcardfront",
        "save & create",
        "move_in",
        "move_out"
    ]):
        return "move_in_out"

    # =====================================================
    # PROPERTY INVENTORY
    # =====================================================
    if any(x in text for x in [
        "asset",
        "asset id",
        "fridge",
        "gas_stove",
        "gas stove",
        "cot",
        "tv_32",
        "center_table",
        "category",
        "description"
    ]):
        return "property_inventory"

    # =====================================================
    # STAFF ATTENDANCE
    # =====================================================
    if any(x in text for x in [
        "trainer name",
        "batch",
        "caretakers",
        "managers name",
        "employee",
        "staff",
        "attendance"
    ]):
        return "staff_attendance"

    # =====================================================
    # INVOICE
    # "receipt" intentionally removed
    # =====================================================
    if any(x in text for x in [
        "invoice",
        "gst",
        "tax invoice",
        "bill",
        "invoice no",
        "total amount",
        "subtotal"
    ]):
        return "invoice"

    # =====================================================
    # PROPERTY
    # =====================================================
    if any(x in text for x in [
        "property",
        "tenant",
        "rent",
        "agreement",
        "room",
        "flat",
        "premises"
    ]):
        return "property"

    # =====================================================
    # UNKNOWN
    # =====================================================
    return "unknown"







    # ---------- OPENAI FALLBACK ----------

    prompt = f"""
You are classifying OCR text from WhatsApp operational images.

You MUST classify into one of these categories:

invoice
payment_receipt
id_proof
visitor_log
geo_tagged_property
property_inventory
move_in_out
staff_attendance
property

Important:
- Most images are operational property-management screenshots.
- Prefer a specific category instead of unknown.
- Use unknown ONLY if the text is completely unreadable or meaningless.

Examples:

Visitor Log / site visit forms -> visitor_log
GPS / Bengaluru / Karnataka / coordinates -> geo_tagged_property
UPI / IMPS / payment successful -> payment_receipt
Asset / fridge / cot / TV -> property_inventory
Move in/out / damages / uploaded files -> move_in_out
Caretaker names / trainer / batch -> staff_attendance
Tenant / room / agreement -> property

Return ONLY the category name.
No explanation.
No sentence.
No punctuation.

OCR TEXT:
{text[:2500]}
"""
    try:
        res = client.responses.create(
            model="gpt-4o-mini",
            input=prompt
        )

        raw_result = (res.output_text or "").strip().lower()

        print(f"🧠 RAW LLM OUTPUT = [{raw_result}]")

        result = normalize_label(raw_result)

        print(f"🧠 NORMALIZED = [{result}]")

        # valid category
        if result in ALLOWED_TYPES:
            return result

        # fallback if model gives weird output
        return "unknown"

    except Exception as e:
        print(f"❌ LLM classification error: {e}")

        # OpenAI failed completely
        return "unknown"   













'''import os
import re
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# ---------------- GEMINI SETUP ----------------

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

model = genai.GenerativeModel("gemini-2.5-flash")

# ---------------- ALLOWED TYPES ----------------

ALLOWED_TYPES = {
    "invoice",
    "payment_receipt",
    "id_proof",
    "visitor_log",
    "geo_tagged_property",
    "property_inventory",
    "move_in_out",
    "staff_attendance",
    "property",
    "unknown",
}


# ---------------- NORMALIZE LABEL ----------------

def normalize_label(label: str) -> str:
    label = (label or "").strip().lower()

    # remove punctuation
    label = re.sub(r"[^a-z0-9_ ]", "", label)

    # normalize spaces
    label = label.replace("-", "_")
    label = label.replace(" ", "_")

    # synonym normalization
    mapping = {
        "geo_tagged": "geo_tagged_property",
        "geo_tagged_image": "geo_tagged_property",
        "geo_property": "geo_tagged_property",
        "property_geo": "geo_tagged_property",

        "visitor": "visitor_log",
        "visitorlog": "visitor_log",

        "payment": "payment_receipt",
        "payment_proof": "payment_receipt",
        "receipt": "payment_receipt",

        "inventory": "property_inventory",

        "moveinout": "move_in_out",
        "movein_out": "move_in_out",

        "staff": "staff_attendance",
        "attendance": "staff_attendance",

        "property_related": "property",
        "building_property": "property",
    }

    label = mapping.get(label, label)

    return label


# ---------------- MAIN CLASSIFIER ----------------

def classify_image_type_from_text(text: str) -> str:
    text = (text or "").strip().lower()

    if not text:
        return "unknown"

    # =========================================================
    # RULES FIRST (FAST + MOST ACCURATE FOR YOUR DATASET)
    # =========================================================

    # ---------- VISITOR LOG ----------

    if any(x in text for x in [
        "visitor log",
        "site visit",
        "visitor contact number",
        "your site visit information",
        "select property",
        "sitevisit",
        "visitor",
        "booking"
    ]):
        print("✅ RULE MATCH = visitor_log")
        return "visitor_log"

    # ---------- PAYMENT RECEIPT ----------

    if any(x in text for x in [
        "payment successful",
        "transaction id",
        "upi",
        "imps",
        "paytm",
        "phonepe",
        "gpay",
        "paid in",
        "sent",
        "payment mode",
        "ref no"
    ]):
        print("✅ RULE MATCH = payment_receipt")
        return "payment_receipt"

    # ---------- ID PROOF ----------

    if any(x in text for x in [
        "aadhaar",
        "aadhar",
        "government of india",
        "permanent account number",
        "driving licence",
        "driving license",
        "voter id",
        "passport"
    ]):
        print("✅ RULE MATCH = id_proof")
        return "id_proof"

    # ---------- MOVE IN / OUT ----------

    if any(x in text for x in [
        "move_in/out",
        "move in/out",
        "no damage",
        "damages",
        "files uploaded",
        "aadharcardfront",
        "save & create",
        "move_in",
        "move_out"
    ]):
        print("✅ RULE MATCH = move_in_out")
        return "move_in_out"

    # ---------- PROPERTY INVENTORY ----------

    if any(x in text for x in [
        "asset",
        "asset id",
        "fridge",
        "gas_stove",
        "gas stove",
        "cot",
        "tv_32",
        "center_table",
        "category",
        "description"
    ]):
        print("✅ RULE MATCH = property_inventory")
        return "property_inventory"

    # ---------- STAFF ATTENDANCE ----------

    if any(x in text for x in [
        "trainer name",
        "batch",
        "caretakers",
        "managers name",
        "employee",
        "staff"
    ]):
        print("✅ RULE MATCH = staff_attendance")
        return "staff_attendance"

    # ---------- GEO TAGGED PROPERTY ----------

    if any(x in text for x in [
        "bengaluru",
        "bangalore division",
        "karnataka",
        "kasavanahalli",
        "btm layout",
        "garvebhavi palya",
        "bommanahalli",
        "latitude",
        "longitude",
        "altitude",
        "google",
        "outer ring rd"
    ]) or re.search(r"\d+\.\d+[ns]\s*\d+\.\d+[ew]", text):

        print("✅ RULE MATCH = geo_tagged_property")
        return "geo_tagged_property"

    # ---------- INVOICE ----------

    if any(x in text for x in [
        "invoice",
        "gst",
        "tax invoice",
        "receipt",
        "bill",
        "invoice no",
        "total amount"
    ]):
        print("✅ RULE MATCH = invoice")
        return "invoice"

    # ---------- PROPERTY ----------

    if any(x in text for x in [
        "property",
        "tenant",
        "rent",
        "agreement",
        "room",
        "flat",
        "premises"
    ]):
        print("✅ RULE MATCH = property")
        return "property"

    # =========================================================
    # GEMINI FALLBACK
    # =========================================================

    prompt = f"""
You are classifying OCR text extracted from WhatsApp operational images.

You MUST classify into EXACTLY one category from this list:

invoice
payment_receipt
id_proof
visitor_log
geo_tagged_property
property_inventory
move_in_out
staff_attendance
property
unknown

Classification Rules:

- invoice:
  bills, GST docs, invoices, tax invoices, vendor receipts

- payment_receipt:
  payment confirmations, UPI, IMPS, bank transfer screenshots,
  Paytm, PhonePe, GPay payment success pages

- id_proof:
  Aadhaar, PAN, voter ID, passport, driving license

- visitor_log:
  visitor entry forms, site visit forms, booking forms,
  visitor logs, visitor registration screenshots

- geo_tagged_property:
  screenshots containing coordinates, GPS, Bengaluru,
  Karnataka, timestamps, maps, addresses, Google map overlays,
  geo verification images

- property_inventory:
  inventory sheets, asset lists, appliance lists,
  room assets, furniture lists

- move_in_out:
  move-in/out inspection forms, damages,
  uploaded proof images, checkout/checkin screenshots

- staff_attendance:
  employee sheets, caretaker names,
  trainer lists, attendance sheets, manager sheets

- property:
  any property-management-related image that does not fit above

- unknown:
  ONLY use if OCR text is unreadable,
  meaningless, empty, or unrelated

IMPORTANT:
- Most WhatsApp images belong to property-management workflows
- Prefer a specific category instead of unknown
- Do NOT explain
- Do NOT add punctuation
- Return ONLY the category name

OCR TEXT:
{text[:2500]}
"""

    try:
        res = model.generate_content(prompt)

        raw_result = (res.text or "").strip().lower()

        print(f"🧠 RAW GEMINI OUTPUT = [{raw_result}]")

        result = normalize_label(raw_result)

        print(f"🧠 NORMALIZED = [{result}]")

        # valid category
        if result in ALLOWED_TYPES:
            return result

        # invalid output
        return "unknown"

    except Exception as e:
        print(f"❌ Gemini classification error: {e}")
        return "unknown"   '''                            