import re
from typing import Optional, Dict
import os
from datetime import datetime
import difflib
from unittest import result

from docx import text
from soupsieve.util import lower

from app.services.ocr_qwen_name_extractor import extract_name_with_qwen


#from app.services.ocr_qwen_name_extractor import extract_name_with_qwen_local



# NER VALIDATION (PRODUCTION SAFE)
# =====================================================

NER_ENABLED = False
ner = None

try:
    from transformers import pipeline

    '''ner = pipeline(
        "ner",
        model="dslim/bert-base-NER",
        aggregation_strategy="simple"
    )'''


    '''ner = pipeline(
        "ner",
        model="ai4bharat/IndicNER",
        aggregation_strategy="simple"
    )'''

    ner = pipeline(
        "ner",
        model="Davlan/xlm-roberta-base-ner-hrl",
        aggregation_strategy="simple"
    )

    NER_ENABLED = True
    print("NER model loaded successfully.")

except Exception as e:
    # If model fails, system will still work
    print(f"NER model not loaded: {e}")
    NER_ENABLED = False


# REGEX
# =====================================================

DIGIT_PATTERN = re.compile(r"\d")
RELATION_PATTERN = re.compile(r"\b[CSDW]\s*/?\s*O\b")
#PIN_PATTERN = re.compile(r"\b\d{6}\b")
PIN_PATTERN = re.compile(r"\d{6}")
NUMERIC_LINE_PATTERN = re.compile(r"[0-9/.\- ]+")


MASKED_AADHAAR_REGEX = r"[Xx]{4,}\d{4}"

PAN_PATTERN = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
DL_PATTERN = re.compile(r"\b[A-Z]{2}-?\d{2}\d{11}\b")
VOTER_PATTERN = re.compile(r"\b[A-Z]{3}[0-9]{7}\b")
#AADHAAR_PATTERN = re.compile(r"\b(?:\d\s*){12}\b")
#AADHAAR_PATTERN = re.compile(r"\b[1-9](?:\d\s*){11}\b")
AADHAAR_PATTERN = re.compile(r"\b[2-9](?:\d[\s.\-]*){11}\b")
#AADHAAR_REGEX = r"\b[2-9]{1}[0-9]{3}\s?[0-9]{4}\s?[0-9]{4}\b"
DATE_PATTERN = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b")
DATE_REGEX = r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b"








def extract_pan_number_smart(text: str) -> Optional[str]:
    upper = text.upper()

    # 1️⃣ strict PAN
    match = PAN_PATTERN.search(upper)
    if match:
        return match.group(0)

    # 2️⃣ OCR-safe PAN pattern
    candidates = re.findall(r"\b[A-Z0-9]{5}[0-9]{4}[A-Z]\b", upper)

    for c in candidates:
        if len(c) != 10:
            continue

        first5 = c[:5]
        mid4 = c[5:9]
        last1 = c[9]

        if not mid4.isdigit():
            continue

        # 🔥 fix OCR confusion
        fixed_first5 = first5.replace("0", "O").replace("1", "I").replace("8", "B")

        if re.fullmatch(r"[A-Z]{5}", fixed_first5) and re.fullmatch(r"[A-Z]", last1):
            return fixed_first5 + mid4 + last1

    return None





# =====================================================
# EXTRA FIELD EXTRACTION (NEW)
# =====================================================

def extract_gender(text: str) -> Optional[str]:
    text_lower = text.lower()

    if "female" in text_lower:
        return "Female"
    if "male" in text_lower:
        return "Male"
    if "transgender" in text_lower:
        return "Transgender"

    return None


def extract_phone(text: str) -> Optional[str]:
    matches = re.findall(r"\b[6-9]\d{9}\b", text)
    return matches[0] if matches else None







def normalize_ocr_noise(text: str) -> str:
    text = text.lower()

    # Common OCR confusions
    replacements = {
        "govemment": "government",
        "govem": "govern",
        "identifcation": "identification",
        "uniqe": "unique",
        "authorty": "authority",
        "aadnar": "aadhaar",
        "aadhar": "aadhaar",
        "incometaxdepartment": "income tax department",
        "permanentaccount": "permanent account",
        "permanentaccountnumber": "permanent account number",
    }

    for wrong, correct in replacements.items():
        text = text.replace(wrong, correct)

    return text




# CLEANING
# =====================================================

def clean_text(text: str) -> str:
    text = text.replace("|", " ")
    text = re.sub(r"[^\x00-\x7F\n]+", " ", text)  
    text = re.sub(r"[ \t]+", " ", text)          
    return text.strip()

# DOCUMENT DETECTION
# =====================================================

def detect_document_type(text: str) -> Optional[str]:

    # 🔹 Normalize OCR noise first
    normalized = normalize_ocr_noise(text)

    upper = normalized.upper()
    lower = normalized.lower()

    scores = {
        "pancard": 0,
        "aadhaar": 0,
        "driving_license": 0,
        "voter_id": 0,
        "nepal_citizenship": 0,
        "student_id": 0,
        "work_id": 0,
    }


    # 1️⃣ PAN DETECTION (LESS STRICT + OCR FRIENDLY)

    pan_score = 0

    # Strict PAN pattern
    if PAN_PATTERN.search(upper):
        pan_score += 8

    # Relaxed PAN-like OCR pattern
    # handles OCR confusion like O ↔ 0 in first 5 chars
    if re.search(r"\b[A-Z0-9]{5}[0-9]{4}[A-Z]\b", upper):
        pan_score += 4

    # PAN keywords
    if "income tax" in lower or "incometaxdepartment" in lower:
        pan_score += 5

    if "permanent account number" in lower or "permanentaccountnumber" in lower:
        pan_score += 4

    if "pan application" in lower:
        pan_score += 3

    if "digitally signed" in lower:
        pan_score += 2

    if "income tax pan services unit" in lower or "nsdl" in lower:
        pan_score += 2

    if "/name" in lower or "father'sname" in lower or "father's name" in lower:
        pan_score += 1

    # Add if strong enough
    if pan_score >= 6:
        scores["pancard"] += pan_score


    # 2️⃣ DRIVING LICENSE

    if DL_PATTERN.search(upper):
        scores["driving_license"] += 8

    if "licence" in lower or "license" in lower:
        scores["driving_license"] += 5


    # 3️⃣ VOTER ID (Improved)

    voter_score = 0

    if VOTER_PATTERN.search(upper):
        voter_score += 6

    if "election commission" in lower:
        voter_score += 6

    if "epic" in lower:
        voter_score += 4

    if voter_score >= 8:
        scores["voter_id"] += voter_score



    # 4️⃣ AADHAAR
    aadhaar_score = 0

    has_12_digit = bool(AADHAAR_PATTERN.search(text))
    has_masked = bool(re.search(MASKED_AADHAAR_REGEX, text))
    has_number = has_12_digit or has_masked

    if "aadhaar" in lower or "aadhar" in lower or "adhar" in lower:
        aadhaar_score += 7

    if "uidai" in lower:
        aadhaar_score += 6

    if "unique identification authority" in lower:
        aadhaar_score += 6

    if "government of india" in lower or "govt of india" in lower or "governmentof india" in lower:
        aadhaar_score += 4

    if "enrolment no" in lower or "enrolmentno" in lower:
        aadhaar_score += 5

    if re.search(r"\b(c|s|d|w)\s*/?\s*o\b", lower):
        aadhaar_score += 3

    if "\nto\n" in f"\n{lower}\n" or lower.startswith("to\n") or "\nto " in f"\n{lower}":
        aadhaar_score += 2

    if "address" in lower:
        aadhaar_score += 2

    if re.search(r"\b(dob|date of birth|year of birth)\b", lower):
        aadhaar_score += 2

    if any(x in lower for x in ["male", "female", "transgender"]):
        aadhaar_score += 1

    if has_12_digit:
        aadhaar_score += 5

    if has_masked:
        aadhaar_score += 5

    if re.search(r"\bvid\s*[:\-]?\s*\d+", lower):
        aadhaar_score += 4

    scores["aadhaar"] += aadhaar_score




    # 5️⃣ OTHER DOCUMENT TYPES

    if "citizenship certificate" in lower:
        scores["nepal_citizenship"] += 7

    if any(x in lower for x in ["university", "academy", "school"]):
        scores["student_id"] += 4

    if any(x in lower for x in ["employee", "company"]):
        scores["work_id"] += 4


    # 6️⃣ SELECT BEST MATCH

    best_doc = max(scores, key=scores.get)
    best_score = scores[best_doc]

    # Minimum confidence threshold
    if best_score >= 6:
        return best_doc

    return None


# UTILITIES
# =====================================================

def sanitize_name(name: str) -> Optional[str]:
    if not name:
        return None

    # Keep original for label-pattern checks before cleanup
    raw_name = name.strip()

    # Hard reject common PAN/OCR label junk before normalization
    if raw_name.lower() in {
        "name", "/name", "a/name", "t/name","c/name", "s/name", "d/name", "w/name",
        "father s name", "father name"
    }:
        return None

    if "incometaxdepartment" in raw_name.lower():
        return None

    if "permanentaccountnumber" in raw_name.lower():
        return None

    # Remove OCR underscore issue (P_I_N_D_A_S_A_H_U → PINDASAHU)
    name = name.replace("_", "")

    # Remove label prefixes like "Name:", "ELECTOR'S NAME:"
    name = re.sub(r"^[A-Za-z/'\s]+:\s*", "", name)

    # Remove unwanted characters
    name = re.sub(r"[^A-Za-z/ ]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()

    if not name:
        return None


     # Reject relation lines at start: C/O, S/O, D/O, W/O
    if re.match(r"^\s*(c|s|d|w)\s*/?\s*o\b", name, re.IGNORECASE):
        return None

    # Reject relation markers anywhere in string
    if re.search(r"\b(c|s|d|w)\s*/?\s*o\b", name, re.IGNORECASE):
        return None

    # Hard reject again after normalization
    if name.lower() in {
        "name", "/name", "a/name", "t/name",
        "father s name", "father name"
    }:
        return None

    if "incometaxdepartment" in name.lower():
        return None

    if "permanentaccountnumber" in name.lower():
        return None

    words = name.split()

    # Too short
    if len(words) == 1 and len(words[0]) <= 2:
        return None

    # Too many words (likely address / garbage)
    if len(words) > 6:
        return None

    blacklist = [
        "government", "india", "department", "authority",
        "enrolment", "unique", "identification",
        "date", "birth", "father", "signature",
        "income", "tax", "permanent", "account",
        "blood", "group",
        "son", "daughter", "wife",
        "name", "elector", "sex", "address",  
        "done", "pan", "pancard", "card", "proof",
        "incometaxdepartment", "permanentaccountnumber"
    ]
    #"c", "s", "w", "d"

    if any(word.lower() in blacklist for word in words):
        return None

    # Reject PAN fragments like GRHPS
    if re.fullmatch(r"[A-Z]{5}", name):
        return None

    # Reject ALL CAPS long OCR headers
    if name.isupper() and len(words) == 1 and len(name) > 6:
        return None

    # Reject repeated character OCR garbage
    for w in words:
        if len(w) > 6 and len(set(w.lower())) <= 2:
            return None

    # Reject unrealistically long words
    if any(len(w) > 20 for w in words):
        return None

    return name




def normalize_name_format(name: str) -> str:
    """
    Normalize OCR name formatting safely.
    Fixes:
    - CamelCase
    - spaced characters (R A N U S H A R M A)
    - ALL CAPS names
    """

    if not name:
        return name

    name = name.strip()

    # 1️⃣ Fix spaced OCR characters
    # Example: R A N U S H A R M A

    words = name.split()

    if all(len(w) == 1 for w in words) and len(words) > 3:
        name = "".join(words)

    # 2️⃣ Fix CamelCase
    # Example: AzhaguganeshM -> Azhaguganesh M

    name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)

    # 3️⃣ Remove extra spaces

    name = re.sub(r'\s+', ' ', name).strip()

    # 4️⃣ Convert ALL CAPS to Title Case

    if name.isupper():
        name = name.title()

    return name


def extract_regex(text: str, pattern) -> Optional[str]:
    match = pattern.search(text)
    if match:
        value = match.group(0)

        # 🔥 FIX: remove ALL whitespace (space, newline, tab)
        value = re.sub(r"\s+", "", value)

        return value.strip()

    return None



# SMART DOB DETECTION
# =====================================================

def extract_dob_from_dates(text: str) -> Optional[str]:

    lines = text.splitlines()
    dates = DATE_PATTERN.findall(text)

    # =====================================================
    # ✅ 0. NEW: YEAR OF BIRTH SUPPORT (TOP PRIORITY)
    # =====================================================
    yob_match = re.search(
        r"(year\s*of\s*birth|yob)[^\d]*(\d{4})",
        text,
        re.IGNORECASE
    )
    if yob_match:
        year = yob_match.group(2)
        return f"01/01/{year}"   # default DOB format

    # =====================================================
    # ❗ IMPORTANT CHANGE: REMOVE EARLY RETURN
    # =====================================================
    # if not dates:
    #     return None

    # =====================================================
    # ✅ 1. PRIORITY: DOB LABEL
    # =====================================================
    dob_match = re.search(
        r"(dob|d0b)[^\d]*(\d{1,2}[/-]\d{1,2}[/-]\d{4})",
        text,
        re.IGNORECASE
    )
    if dob_match:
        return dob_match.group(2)

    # =====================================================
    # ✅ 2. DOB NEARBY CONTEXT
    # =====================================================
    for i, line in enumerate(lines):
        if "dob" in line.lower():
            for d in dates:
                if d in line:
                    return d

            for j in range(max(0, i-1), min(len(lines), i+2)):
                for d in dates:
                    if d in lines[j]:
                        return d

    # =====================================================
    # ❗ If still no dates → RETURN None
    # =====================================================
    if not dates:
        return None

    # =====================================================
    # ✅ 3. FILTER VALID DATES
    # =====================================================
    current_year = datetime.now().year
    min_year = current_year - 120

    valid_dates = []

    for d in dates:
        try:
            year = int(d.split("/")[-1] if "/" in d else d.split("-")[-1])

            if min_year <= year <= current_year:
                valid_dates.append((d, year))

        except:
            continue

    if not valid_dates:
        return None

    # =====================================================
    # ✅ 4. PICK OLDEST (fallback)
    # =====================================================
    valid_dates.sort(key=lambda x: x[1])
    return valid_dates[0][0]





# STRONG VOTER EXTRACTION (NEW)
# =====================================================

def extract_voter_strong(text: str) -> Dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    upper = text.upper()

    id_number = None
    name = None
    dob = None

    # 1️⃣ EPIC Number Extraction
    epic_match = VOTER_PATTERN.search(upper)
    if epic_match:
        id_number = epic_match.group(0)

    #  2️⃣ STRICT: ELECTOR'S NAME: XYZ (INLINE FORMAT)
    
    inline_match = re.search(
        r"ELECTOR.?S\s*NAME[:\s]*([A-Z]+)",
        upper
    )
    if inline_match:
        candidate = sanitize_name(inline_match.group(1))
        if candidate:
            name = candidate

    #  3️⃣ Handle OCR merged cases (e.g., SNAME PREMRAJTHAKUR)
    if not name:
        for line in lines:
            cleaned_line = re.sub(r"^[A-Z]?NAME[:\s]*", "", line.upper())

            if "NAME" in line.upper():
                words = re.findall(r"[A-Z]{2,}", cleaned_line)
                if words:
                    candidate = sanitize_name(" ".join(words))
                    if candidate:
                        name = candidate
                        break

    #  4️⃣ Label line then next line format 
    if not name:
        for i, line in enumerate(lines):

            # EXACT MATCH CASE
            if line.lower() in ["elector's name", "name"]:

                if i + 1 < len(lines):
                    candidate = lines[i + 1].strip()

                    # 🔥 remove leading colon
                    candidate = re.sub(r"^[:\s]+", "", candidate)

                    # ❌ Skip if contains digits
                    if not DIGIT_PATTERN.search(candidate):
                        cleaned = sanitize_name(candidate)
                        if cleaned:
                            name = cleaned
                            break

            # OCR variation case (elector s name, elector name)
            if re.search(r"elector.?s name", line.lower()):
                if i + 1 < len(lines):
                    candidate = lines[i + 1].strip()
                    candidate = re.sub(r"^[:\s]+", "", candidate)

                    if not DIGIT_PATTERN.search(candidate):
                        cleaned = sanitize_name(candidate)
                        if cleaned:
                            name = cleaned
                            break

    # 5️⃣ DOB Extraction
    dob_match = DATE_PATTERN.search(text)
    if dob_match:
        dob = dob_match.group(0)

    return {
        "id_number": id_number,
        #"is_valid_id": bool(id_number),
        "is_valid_id": True if id_number else False,
        "name": name,
        "dob": dob,
        "address": None
    }

# SMART NAME EXTRACTION
# =====================================================

def extract_name_smart(text: str, id_number: Optional[str]) -> Optional[str]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    blacklist = [
        "union", "india", "licence", "license",
        "transport", "valid", "issue",
        "blood", "govt", "son", "daughter", "wife",
        "date", "birth", "male", "female",
        "gender", "address", "principal",
        "registrar", "course", "batch",
        "section", "student", "identity",
        "card", "university", "academy",
        "school", "campus", "signatory"
    ]


    # STEP 0: STRONG RULE (Aadhaar / Indian IDs)
    # Prefer name before S/O, D/O, W/O
    # ====================================================
    for i, line in enumerate(lines):
        upper = line.upper()

        if "S/O" in upper or "D/O" in upper or "W/O" in upper:
            if i > 0:
                candidate = lines[i - 1]
                lower = candidate.lower()

                # Skip invalid lines
                if DIGIT_PATTERN.search(candidate):    
                    continue

                if any(b in lower for b in blacklist):
                    continue

                words = re.findall(r"[A-Za-z]{2,}", candidate)

                if 1 <= len(words) <= 3:
                    return sanitize_name(" ".join(words))

    # STEP 1: Try name after DOB
    for i, line in enumerate(lines):
        if DATE_PATTERN.search(line):
            for j in range(i + 1, min(i + 5, len(lines))):
                candidate = lines[j]
                lower = candidate.lower()

                if DIGIT_PATTERN.search(candidate):
                    continue

                if any(b in lower for b in blacklist):
                    continue

                words = re.findall(r"[A-Za-z]{2,}", candidate)

                if 1 <= len(words) <= 3:
                    return sanitize_name(" ".join(words))

    # STEP 2: General fallback heuristic
    candidates = []

    for line in lines:
        lower = line.lower()

        if id_number and id_number in line:
            continue

        if DIGIT_PATTERN.search(line):
            continue

        if any(b in lower for b in blacklist):
            continue

        words = re.findall(r"[A-Za-z]{2,}", line)

        if 1 <= len(words) <= 3:
            candidate = " ".join(words)

            # Reject Gender explicitly
            if candidate.upper() in ["MALE", "FEMALE"]:
                continue

            # Reject single uppercase noise words
            if len(words) == 1 and candidate.isupper():
                continue

            candidates.append(candidate)

    if not candidates:
        return None

    # Prefer Title Case (Aadhaar usually Title Case)
    title_case = [c for c in candidates if c.istitle()]
    if title_case:
        return sanitize_name(title_case[0])

    # Prefer shortest realistic candidate
    return sanitize_name(min(candidates, key=len))


# DRIVING LICENSE
# =====================================================

def extract_dl_data(text: str) -> Dict:
    upper = text.upper()
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    id_number = None
    dob = None
    name = None
    address = None

    # 1️⃣ ID NUMBER
    id_match = DL_PATTERN.search(upper)
    if id_match:
        id_number = id_match.group(0)

    # 2️⃣ DOB
    dob_match = re.search(r"DOB[:\s]*" + DATE_REGEX, upper)
    if dob_match:
        dob = DATE_PATTERN.search(dob_match.group(0)).group(0)
    else:
        dob = extract_dob_from_dates(text)

    #  3️⃣ INLINE FORMAT (Name:XYZ)
    inline_match = re.search(r"\bNAME[:\s]*([A-Z ]{3,})", upper)
    if inline_match:
        candidate = inline_match.group(1).strip()
        candidate = re.sub(r"[^A-Z ]", "", candidate)

        cleaned = sanitize_name(candidate)
        if cleaned and cleaned.lower() not in [
            "name", "date of birth", "blood group"
        ]:
            name = cleaned

    #  4️⃣ MULTI-LINE NAME RULE
    if not name:
        for i, line in enumerate(lines):

            if line.strip().lower() == "name":

                for j in range(i + 1, min(i + 8, len(lines))):
                    candidate = lines[j]

                    # ❌ Skip dates
                    #if re.search(DATE_REGEX, candidate):
                    if DATE_PATTERN.search(candidate):
                        continue

                    # ❌ Skip numeric lines
                    #if re.search(r"\d", candidate):
                    if DIGIT_PATTERN.search(candidate):
                        continue

                    # ❌ Skip relationship markers
                    if any(x in candidate.lower() for x in [
                        "son", "daughter", "wife",
                        "s/w/d", "s w d"
                    ]):
                        continue

                    # ❌ Skip known DL labels
                    if any(x in candidate.lower() for x in [
                        "blood", "group", "bg",
                        "issue", "validity",
                        "authorisation"
                    ]):
                        continue

                    cleaned = sanitize_name(candidate)

                    # ❌ Final safety
                    if not cleaned:
                        continue

                    name = cleaned
                    break

                break

    #  5️⃣ NAME BEFORE DOB 
    if not name and dob:
        for i, line in enumerate(lines):
            if dob in line:
                for offset in [1, 2]:
                    if i - offset >= 0:
                        candidate = sanitize_name(lines[i - offset])
                        if candidate:
                            name = candidate
                            break
                break

    #  6️⃣ FINAL FALLBACK
    if not name:
        temp = extract_name_smart(text, id_number)
        if temp:
            name = temp

    # 7️⃣ ADDRESS
    address_match = re.search(r"ADDRESS[:\s]*([A-Z0-9,/ -]+)", upper)
    if address_match:
        address = address_match.group(1).strip()

    return {
        "id_number": id_number,
        #"is_valid_id": bool(id_number),
        "is_valid_id": True if id_number else False,
        "name": name,
        "dob": dob,
        "address": address
    }


#===========================================================================================================================================









PAN_NAME_BLACKLIST = {
    "fa htt", "fa HT", 
    "fa htst",
    "f ahtst",
    "f ahtnt",
    "pan pdf",
    "efa HTST",
    "efa H" 
}

def is_pan_blacklisted(name: str) -> bool:
    if not name:
        return False

    normalized = name.lower().strip()

    return normalized in PAN_NAME_BLACKLIST


def looks_like_pan_letters(name: str, pan: str = None):

    if not name:
        return False

    letters = name.replace(" ", "")

    # If letters match first 5 + last PAN letter
    if pan:
        pan_letters = pan[:5] + pan[-1]
        if letters == pan_letters:
            return True

    # General pattern (6 uppercase letters)
    if len(letters) == 6 and letters.isupper():
        return True

    return False












def is_strong_pan_name_candidate(line: str, pan: str = None) -> bool:
    if not line:
        return False

    raw = line.strip()
    lower = raw.lower()

    if any(ch.isdigit() for ch in raw):
        return False

    if any(x in lower for x in [
        "income", "tax", "department", "government", "govt",
        "permanent", "account", "number", "signature",
        "father", "dob", "date of birth", "name",
        "incometaxdepartment", "permanentaccountnumber"
    ]):
        return False

    candidate = sanitize_name(normalize_name_format(raw))
    if not candidate:
        return False

    if is_pan_blacklisted(candidate):
        return False

    if looks_like_pan_letters(candidate, pan):
        return False

    words = candidate.split()

    # PAN names are usually 2-4 words
    if not (2 <= len(words) <= 4):
        return False

    # uppercase OCR line preferred
    if not raw.isupper():
        return False

    return True






def get_pan_uppercase_name_candidates(lines, pan: str = None, dob: str = None):
    candidates = []

    cutoff = min(len(lines), 8)

    # do not cut to 0 when DOB is the first line
    if dob:
        for i, line in enumerate(lines):
            if dob in line:
                if i > 2:
                    cutoff = i
                break

    for line in lines[:cutoff]:
        if is_strong_pan_name_candidate(line, pan):
            cleaned = sanitize_name(normalize_name_format(line))
            if cleaned:
                candidates.append(cleaned)

    # also scan full OCR near label noise, because many PAN OCRs are not pure uppercase
    for line in lines:
        raw = line.strip()
        lower = raw.lower()

        if any(ch.isdigit() for ch in raw):
            continue

        if any(x in lower for x in [
            "income", "tax", "department", "government", "govt",
            "permanent", "account", "number", "signature"
        ]):
            continue

        candidate = sanitize_name(normalize_name_format(raw))
        if not candidate:
            continue

        if is_pan_blacklisted(candidate):
            continue

        if looks_like_pan_letters(candidate, pan):
            continue

        words = candidate.split()
        if 2 <= len(words) <= 4:
            candidates.append(candidate)

    seen = set()
    final_candidates = []
    for c in candidates:
        key = c.lower()
        if key not in seen:
            seen.add(key)
            final_candidates.append(c)

    return final_candidates






PAN_FATHER_LABEL_RE = re.compile(r"father.?s\s*name", re.IGNORECASE)
PAN_NAME_LABEL_RE = re.compile(r"(?:^|[/\s])name(?:\s|$)", re.IGNORECASE)
PAN_DOB_LABEL_RE = re.compile(r"(date\s*of\s*birth|\bdob\b)", re.IGNORECASE)


def is_pan_name_candidate_line(raw_line: str, pan: str = None) -> Optional[str]:
    if not raw_line:
        return None

    raw = raw_line.strip()
    lower = raw.lower()

    # hard skip noisy label lines
    if any(x in lower for x in [
        "income", "tax", "department", "government", "govt",
        "permanent", "account", "number", "signature",
        "digitally", "signed", "valid unless", "card",
        "date of birth", "dob"
    ]):
        return None

    # skip explicit label lines themselves
    if PAN_FATHER_LABEL_RE.search(raw):
        return None

    # allow "/Name" label line to be skipped
    normalized_label = re.sub(r"[^a-z]", "", lower)
    if normalized_label in {"name"}:
        return None

    # PAN number fragments / numeric lines
    if any(ch.isdigit() for ch in raw):
        return None

    candidate = sanitize_name(normalize_name_format(raw))
    if not candidate:
        return None

    if looks_like_label_noise(candidate):
        return None

    if is_pan_blacklisted(candidate):
        return None

    if looks_like_pan_letters(candidate, pan):
        return None

    words = candidate.split()

    # Allow:
    # - single long genuine names like CHRISTUDOSS
    # - normal 2-4 word names
    # - names with initials like DARSHAN D S
    if len(words) == 1:
        if len(words[0]) < 5:
            return None
    elif len(words) > 4:
        return None

    # reject overly noisy initials-only text
    non_trivial_words = [w for w in words if len(w) >= 2]
    if not non_trivial_words:
        return None

    return candidate


def rank_pan_holder_candidates(lines, pan: str = None, dob: str = None):
    """
    Rank likely PAN holder names from OCR text.
    This is ONLY a fallback if Qwen returns NULL.
    """
    candidates = []
    dob_idxs = []
    father_label_idxs = []
    name_label_idxs = []

    for i, line in enumerate(lines):
        lower = line.lower().strip()

        if dob and dob in line:
            dob_idxs.append(i)

        if PAN_DOB_LABEL_RE.search(lower):
            dob_idxs.append(i)

        if PAN_FATHER_LABEL_RE.search(lower):
            father_label_idxs.append(i)

        # only holder-name label, not father's name label
        if "/name" in lower or (PAN_NAME_LABEL_RE.search(lower) and not PAN_FATHER_LABEL_RE.search(lower)):
            name_label_idxs.append(i)

    dob_idxs = sorted(set(dob_idxs))
    father_label_idxs = sorted(set(father_label_idxs))
    name_label_idxs = sorted(set(name_label_idxs))

    for i, raw in enumerate(lines):
        candidate = is_pan_name_candidate_line(raw, pan=pan)
        if not candidate:
            continue

        score = 0
        words = candidate.split()

        # base score
        if 2 <= len(words) <= 4:
            score += 4
        elif len(words) == 1:
            score += 1

        if raw.isupper():
            score += 2

        if candidate.istitle():
            score += 1

        # good for names with trailing initials like DARSHAN D S
        if len(words) >= 2 and any(len(w) == 1 for w in words[1:]):
            score += 1

        # slight penalty for leading initial like "C NIRMAL ..."
        if len(words) >= 2 and len(words[0]) == 1:
            score -= 2

        # strongest: line immediately before "/Name"
        for j in name_label_idxs:
            if i == j - 1:
                score += 8
            elif abs(i - j) == 2:
                score += 3

        # useful: line before father's label often holder name
        # line immediately after father's label often father name
        for j in father_label_idxs:
            if i == j - 1:
                score += 7
            elif i == j + 1:
                score -= 9
            elif i == j + 2:
                score += 4
            elif i == j - 2:
                score += 3

        # near DOB is often useful
        for j in dob_idxs:
            if i == j + 1 or i == j - 1:
                score += 6
            elif i == j + 2 or i == j - 2:
                score += 3

        candidates.append({
            "index": i,
            "raw": raw,
            "name": candidate,
            "score": score,
        })

    # dedupe by normalized name, keep best score
    best_by_name = {}
    for item in candidates:
        key = item["name"].lower()
        if key not in best_by_name or item["score"] > best_by_name[key]["score"]:
            best_by_name[key] = item

    ranked = sorted(
        best_by_name.values(),
        key=lambda x: (-x["score"], x["index"])
    )

    return ranked


def get_pan_rule_fallback_name(lines, pan: str = None, dob: str = None):
    ranked = rank_pan_holder_candidates(lines, pan=pan, dob=dob)

    if not ranked:
        return None, []

    best_name = ranked[0]["name"]
    all_names = [x["name"] for x in ranked]

    return best_name, all_names












def extract_pan_name_strong(lines: list, pan: str = None) -> Optional[str]:
    for i, line in enumerate(lines):
        lower = line.lower()

        # Case 1: "/Name" label
        if "/name" in lower or lower.strip() == "name":

            for j in range(i + 1, min(i + 3, len(lines))):
                candidate = lines[j].strip()

                if any(ch.isdigit() for ch in candidate):  # Skip lines with numbers
                    continue

                cleaned = sanitize_name(normalize_name_format(candidate))

                if cleaned and not looks_like_pan_letters(cleaned, pan):  # Skip if it looks like PAN letters
                    return cleaned

        # Case 2: Father's name → take previous line
        if "father" in lower:
            if i > 0:
                candidate = lines[i - 1].strip()

                if any(ch.isdigit() for ch in candidate):  # Skip lines with numbers
                    continue

                cleaned = sanitize_name(normalize_name_format(candidate))

                if cleaned and not looks_like_pan_letters(cleaned, pan):  # Skip if it looks like PAN letters
                    return cleaned

    return None


















#PAN EXTRACTION
#=====================================================================================================================================



'''def extract_pan_data(text: str) -> Dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    id_number = extract_pan_number_smart(text)
    dob = None

    for line in lines:
        match = DATE_PATTERN.search(line)
        if match:
            dob = match.group(0)
            break

    pan_rule_fallback_name, pan_name_candidates = get_pan_rule_fallback_name(
        lines,
        pan=id_number,
        dob=dob
    )

    return {
        "id_number": id_number,
        "is_valid_id": bool(id_number),
        "name": None,   # PAN stays Qwen-first
        "dob": dob,
        "address": None,
        "document_type": "pancard",
        "needs_qwen_name": True,
        "pan_rule_fallback_name": pan_rule_fallback_name,
        "pan_name_candidates": pan_name_candidates,
    }'''







# PAN
# =====================================================

def extract_pan_data(text: str) -> Dict:

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    upper_lines = [l.upper() for l in lines]

    id_number = None
    dob = None
    name = None

    # 1️⃣ Extract PAN Number
    
    
    # 1️⃣ Extract PAN Number (OCR SAFE)
    id_number = extract_pan_number_smart(text)

    # 2️⃣ Extract DOB
    for line in lines:
        match = DATE_PATTERN.search(line)
        if match:
            dob = match.group(0)
            break


    # 3️⃣ STRONG LABEL RULE — Line after "/Name"
    if not name:
        for i, line in enumerate(lines):

            if "/name" in line.lower() or line.lower().strip() == "name":

                if i + 1 < len(lines):

                    #candidate = sanitize_name(lines[i + 1])
                    candidate = sanitize_name(normalize_name_format(lines[i + 1]))

                    if not candidate:
                        continue

                    if is_pan_blacklisted(candidate):
                        continue

                    if any(len(w) <= 2 for w in candidate.split()):
                        continue

                    if candidate.lower() in ["pan", "pdf", "signature"]:
                        continue

                    if looks_like_pan_letters(candidate, id_number):
                        continue

                    if lines[i + 1].isupper():
                        name = candidate
                        break

    

    # 3B️⃣ STRONG RULE — look near /Name label
    if not name:
        for i, line in enumerate(lines):
            if "/name" in line.lower() or line.strip().lower() == "name":

                for j in range(i + 1, min(i + 3, len(lines))):
                    raw_candidate = lines[j].strip()

                    if raw_candidate.lower() in ["/name", "a/name", "t/name", "name"]:
                        continue

                    candidate = sanitize_name(
                        normalize_name_format(raw_candidate)
                    )

                    if not candidate:
                        continue

                    if is_pan_blacklisted(candidate):
                        continue

                    if looks_like_pan_letters(candidate, id_number):
                        continue

                    if any(len(w) <= 1 for w in candidate.split()):
                        continue

                    if candidate.lower() in [
                        "name", "signature", "father s name", "father name",
                        "pan", "pdf"
                    ]:
                        continue

                    lower_line = raw_candidate.lower()

                    if any(x in lower_line for x in [
                        "father", "signature", "government", "income",
                        "tax", "department", "permanent", "account"
                    ]):
                        continue

                    name = candidate
                    break

                if name:
                    break






    # 4️⃣ STRONG RULE — Uppercase Multi-word Name (Top Area)
    if not name:

        for line in lines[:8]:

            cleaned = sanitize_name(line)

            if not cleaned:
                continue

            if is_pan_blacklisted(cleaned):
                continue

            word_count = len(cleaned.split())

            if 2 <= word_count <= 4 and line.isupper():

                if id_number and id_number in line:
                    continue

                if any(len(w) <= 2 for w in cleaned.split()):
                    continue

                if cleaned.lower() in ["pan", "pdf", "signature"]:
                    continue

                if looks_like_pan_letters(cleaned, id_number):
                    continue

                lower_line = line.lower()

                if any(x in lower_line for x in [
                    "govt", "government",
                    "income", "tax", "department",
                    "permanent", "account",
                    "signature",
                    "services", "unit",
                    "floor", "chambers",
                    "exchange", "near",
                    "tel", "fax", "email",
                    "nsdl", "incometaxdepartment", "permanentaccountnumber"
                ]):
                    continue

                name = cleaned
                break


    # 5️⃣ Name Above Father's Name
    if not name:

        for i, line in enumerate(lines):

            if "father" in line.lower():

                if i > 0:

                    #candidate = sanitize_name(lines[i - 1])
                    candidate = sanitize_name(normalize_name_format(lines[i - 1]))

                    if not candidate:
                        continue

                    if is_pan_blacklisted(candidate):
                        continue

                    word_count = len(candidate.split())

                    if 1 <= word_count <= 4:

                        if any(len(w) <= 2 for w in candidate.split()):
                            continue

                        if candidate.lower() in ["pan", "pdf", "signature"]:
                            continue

                        if looks_like_pan_letters(candidate, id_number):
                            continue

                        name = candidate
                        break


    # 6️⃣ STRICT LOCATION FALLBACK
    if not name:

        pan_index = None
        dob_index = None

        for i, line in enumerate(lines):

            if id_number and id_number in line:
                pan_index = i

            if dob and dob in line:
                dob_index = i

        if dob_index is not None:
            cutoff = dob_index
        elif pan_index is not None:
            cutoff = pan_index
        else:
            cutoff = len(lines)

        for i in range(0, cutoff):

            candidate = sanitize_name(lines[i])

            if not candidate:
                continue

            if is_pan_blacklisted(candidate):
                continue

            word_count = len(candidate.split())

            if 1 <= word_count <= 4:

                if any(len(w) <= 2 for w in candidate.split()):
                    continue

                if candidate.lower() in ["pan", "pdf", "signature"]:
                    continue

                if looks_like_pan_letters(candidate, id_number):
                    continue

                lower_line = lines[i].lower()

                if any(x in lower_line for x in [
                    "govt", "government",
                    "income", "tax", "department",
                    "permanent", "account",
                    "signature", "services", "unit",
                    "floor", "chambers",
                    "exchange", "near",
                    "tel", "fax", "email",
                    "nsdl"
                ]):
                    continue

                name = candidate
                break


    return {
        "id_number": id_number,
        #"is_valid_id": bool(id_number),
        "is_valid_id": True if id_number else False,
        "name": name,
        "dob": dob,
        "address": None,
        "document_type": "pancard"
    }




#=============================Aadhaar CARD CHECK============================================================






def is_part_of_aadhaar(pin, text):
    aadhaar_numbers = re.findall(r"\d{4}\s*\d{4}\s*\d{4}", text)

    for a in aadhaar_numbers:
        if pin in a.replace(" ", ""):
            return True

    return False



'''ADDRESS_SKIP_WORDS = [
    "uidai", "government", "government of india", "unique identification",
    "your aadhaar no", "dob", "male", "female", "vid", "mobile",
    "aadhaar is proof", "authentication", "verify", "www.uidai", "help@",
    "aadhaar helps", "download maadhaar", "keep your mobile", "entities seeking aadhaar",
    "offline xml", "qr code", "scan", "verified", "digitally", "issue date",
    "download date", "p.o.box", "1947",
    "documents to support identity",
    "should be updated in aadhaar",
    "after every 10 years",
    "government benefits",
    "non-government benefits",
    "lock/unlock aadhaar",
    "biometrics",
    "carry aadhaar",
    "myaadhaar",
    "m aadhaar",
    "aadhaar services",
    "proof of identity",
    "proof of citizenship",
    "please update your mobile",
    "email id updated"
]'''



ADDRESS_SKIP_WORDS = [
    "uidai", "government", "government of india", "unique identification",
    "unique identification authority", "unique identification authority of india",
    "your aadhaar no", "aadhaar no", "aadhaar number", "dob", "male", "female",
    "vid", "mobile", "qr code", "secure qr code", "offline xml", "authentication",
    "verify", "verified", "www.uidai", "uidai.gov", "help@", "1947",
    "aadhaar is proof", "proof of identity", "proof of citizenship",
    "proof of identity not", "not of citizenship", "date of birth",
    "aadhaar helps", "government benefits", "non-government benefits",
    "download maadhaar", "download myaadhaar", "download m aadhaar",
    "keep your mobile", "email id updated", "entities seeking aadhaar",
    "documents to support identity", "documents to support identity and address",
    "should be updated in aadhaar", "after every 10 years",
    "lock/unlock aadhaar", "biometrics", "aadhaar services",
    "carry aadhaar", "myaadhaar", "m aadhaar", "consent",
    "appointed authentication agency", "avail of aadhaar services",
    "when not using aadhaar", "obligated to seek consent",
    "download date", "issue date", "issued:", "aadhaar letter should be verified",
    "scanning of qr code", "online authentication",
    "this aadhaar letter should be verified",
    "aadhaar is unique and secure",
    "keep your mobile number and email id updated",
    "aadhaar after every 10 years",
    "from date of enrolment for aadhaar",
]

INDIA_STATES = [
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh",
    "goa", "gujarat", "haryana", "himachal pradesh", "jharkhand", "karnataka",
    "kerala", "madhya pradesh", "maharashtra", "manipur", "meghalaya", "mizoram",
    "nagaland", "odisha", "orissa", "punjab", "rajasthan", "sikkim", "tamil nadu",
    "telangana", "tripura", "uttar pradesh", "uttarakhand", "west bengal",
    "andaman and nicobar islands", "chandigarh", "dadra and nagar haveli and daman and diu",
    "delhi", "jammu and kashmir", "ladakh", "lakshadweep", "puducherry"
]


def is_relation_line(line: str) -> bool:
    return bool(re.search(r"^\s*(c|s|d|w)\s*/?\s*o\b", line or "", re.IGNORECASE))



'''def should_skip_address_line(line: str) -> bool:
    lower = (line or "").lower().strip()
    if not lower:
        return True

    if len(lower) < 3:
        return True

    if any(x in lower for x in ADDRESS_SKIP_WORDS):
        return True

    if DATE_PATTERN.search(lower):
        return True

    if re.search(MASKED_AADHAAR_REGEX, line or ""):
        return True

    # skip Aadhaar footer/help style lines
    if re.search(r"\b(help@|www\.|uidai\.gov|1947)\b", lower):
        return True

    # skip heavy policy text lines
    if len(lower.split()) > 10 and (
        "aadhaar" in lower or "authentication" in lower or "identity" in lower
    ):
        return True

    return False'''

def should_skip_address_line(line: str) -> bool:
    lower = (line or "").lower().strip()
    if not lower:
        return True

    if len(lower) < 3:
        return True

    if any(x in lower for x in ADDRESS_SKIP_WORDS):
        return True

    if DATE_PATTERN.search(lower):
        return True

    if re.search(MASKED_AADHAAR_REGEX, line or ""):
        return True

    if re.search(r"\b(help@|www\.|uidai\.gov|1947)\b", lower):
        return True

    # Skip pure policy/info lines
    if len(lower.split()) > 7 and any(x in lower for x in [
        "aadhaar", "authentication", "identity", "citizenship",
        "verification", "services", "benefits", "biometrics", "consent"
    ]):
        return True

    # Skip lines that look like OCR garbage with very low usable signal
    alpha_words = re.findall(r"[a-zA-Z]{3,}", lower)
    if len(alpha_words) == 0:
        return True

    return False


def is_address_stop_line(line: str) -> bool:
    lower = (line or "").lower().strip()
    if not lower:
        return True

    if should_skip_address_line(line):
        return True

    if any(x in lower for x in [
        "your aadhaar no", "vid", "government of india",
        "aadhaar is proof", "download", "verify", "authentication",
        "help@", "www.uidai", "1947", "aadhaar helps",
        "entities seeking aadhaar", "lock/unlock aadhaar",
        "should be updated in aadhaar", "after every 10 years",
        "proof of identity", "proof of citizenship"
    ]):
        return True

    return False





'''def extract_state_pincode_fallback(text: str):
    lower = (text or "").lower()
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]

    # ---------------- PINCODE from plain text ----------------
    pins = re.findall(r"\b\d{6}\b", text or "")
    valid_pins = []

    for pin in pins:
        if not is_part_of_aadhaar(pin, text or "") and pin != "000000":
            valid_pins.append(pin)

    pincode = valid_pins[-1] if valid_pins else None

    state = None
    source = None

    # ---------------- STEP 1: structured lines first ----------------
    priority_lines = []
    for line in lines:
        line_lower = line.lower()
        if any(x in line_lower for x in [
            "state", "pin", "pincode", "district", "dist", "address", "po", "vtc"
        ]):
            priority_lines.append(line)

    for line in priority_lines:
        for s in INDIA_STATES:
            pattern = r"(?<![a-zA-Z])" + re.escape(s).replace(r"\ ", r"\s+") + r"(?![a-zA-Z])"
            if re.search(pattern, line.lower(), re.IGNORECASE):
                state = STATE_ALIASES.get(s, s.title())
                source = "structured_text"
                break
        if state:
            break

    # ---------------- STEP 2: full plain text scan ----------------
    if not state:
        for s in INDIA_STATES:
            pattern = r"(?<![a-zA-Z])" + re.escape(s).replace(r"\ ", r"\s+") + r"(?![a-zA-Z])"
            if re.search(pattern, lower, re.IGNORECASE):
                state = STATE_ALIASES.get(s, s.title())
                source = "global_text"
                break

    # ---------------- STEP 3: pincode fallback only if state missing ----------------
    if not state and pincode:
        pin_state = get_state_from_pincode(pincode)
        if pin_state:
            state = pin_state
            source = "pincode"

    return state, pincode, source'''

def extract_state_pincode_fallback(text: str):
    lower = (text or "").lower()
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]

    # pincode from plain_text
    pins = re.findall(r"\b\d{6}\b", text or "")
    valid_pins = []

    for pin in pins:
        if pin != "000000" and not is_part_of_aadhaar(pin, text or ""):
            valid_pins.append(pin)

    pincode = valid_pins[-1] if valid_pins else None

    state = None
    source = None

    # 1. structured lines first
    priority_lines = []
    for line in lines:
        line_lower = line.lower()
        if any(x in line_lower for x in [
            "state", "pin", "pincode", "district", "dist", "address", "po", "vtc"
        ]):
            priority_lines.append(line)

    # also include lines containing a pincode, because many OCR lines look like:
    # MuzaffarpurBihar-843107
    for line in lines:
        if re.search(r"\b\d{6}\b", line) and line not in priority_lines:
            priority_lines.append(line)

    for line in priority_lines:
        # normal match
        for s in INDIA_STATES:
            pattern = r"(?<![a-zA-Z])" + re.escape(s).replace(r"\ ", r"\s+") + r"(?![a-zA-Z])"
            if re.search(pattern, line.lower(), re.IGNORECASE):
                state = STATE_ALIASES.get(s, s.title())
                source = "structured_text"
                break

        # joined OCR match like MuzaffarpurBihar-843107
        if not state:
            compact = re.sub(r"\s+", "", line).lower()
            for s in INDIA_STATES:
                key = s.replace(" ", "").lower()
                pattern = re.escape(key) + r"(?=(?:[-,:/ ]?\d{6})|[-,:/ ]|$)"
                if re.search(pattern, compact, re.IGNORECASE):
                    state = STATE_ALIASES.get(s, s.title())
                    source = "structured_text"
                    break

        if state:
            break

    # 2. whole plain_text fallback
    if not state:
        for s in INDIA_STATES:
            pattern = r"(?<![a-zA-Z])" + re.escape(s).replace(r"\ ", r"\s+") + r"(?![a-zA-Z])"
            if re.search(pattern, lower, re.IGNORECASE):
                state = STATE_ALIASES.get(s, s.title())
                source = "global_text"
                break

    # joined full-text fallback
    if not state:
        compact_text = re.sub(r"\s+", "", text or "").lower()
        for s in INDIA_STATES:
            key = s.replace(" ", "").lower()
            pattern = re.escape(key) + r"(?=(?:[-,:/ ]?\d{6})|[-,:/ ]|$)"
            if re.search(pattern, compact_text, re.IGNORECASE):
                state = STATE_ALIASES.get(s, s.title())
                source = "global_text"
                break

    return state, pincode, source



def strip_relation_prefix(line: str) -> str:
    if not line:
        return ""

    # remove only the relation-name prefix at the beginning
    line = re.sub(
        r"^\s*(?:c|s|d|w)\s*/?\s*o\s*:?\s*[^,]+,?\s*",
        "",
        line,
        flags=re.IGNORECASE
    )
    return line.strip()


#+++++=================================Aadhaar State/Pincode Extraction (NEW)========================================
def is_weak_state_match(state: str, text: str) -> bool:
    if not state:
        return True

    text_lower = text.lower()
    state_lower = state.lower()

    # ✅ strong if exact present
    if state_lower in text_lower:
        return False

    # ✅ strong if joined OCR match (andamanandnicobar)
    compact_text = re.sub(r"\s+", "", text_lower)
    compact_state = state_lower.replace(" ", "")

    if compact_state in compact_text:
        return False

    # ❌ weak if very short (like "goa")
    if len(state_lower) <= 4:
        return True

    return True









def resolve_state_pincode_from_text(text: str, address: Optional[str] = None):
    state, pincode = None, None
    state_source = None

    # 1. try from extracted address first
    if address:
        addr_state, addr_pin = extract_address_components(address, text)
        if addr_state:
            state = addr_state
            state_source = "address"
        if addr_pin:
            pincode = addr_pin

    # 2. if state missing or pincode missing, go to fallback plain_text
    if not state or not pincode:
        fb_state, fb_pin, fb_source = extract_state_pincode_fallback(text)

        if not state and fb_state:
            state = fb_state
            state_source = fb_source

        if not pincode and fb_pin:
            pincode = fb_pin

    # 3. pincode support/fallback only
    '''if pincode:
        #pin_state = PINCODE_STATE_MAP.get(pincode[:3]) or PINCODE_STATE_MAP.get(pincode[:2])
        pin_state = PINCODE_STATE_MAP_3.get(pincode[:3]) or PINCODE_STATE_MAP_2.get(pincode[:2])

        if pin_state:
            # if no state found anywhere, use pin_state
            if not state:
                state = pin_state
                state_source = "pincode"

            # only weak global_text state can be corrected by pincode
            elif state_source == "global_text" and state.lower() != pin_state.lower():
                state = pin_state
                state_source = "pincode"'''

            # address / structured_text state should not be overwritten
            # 


    # =========================================
    # STEP 3: SMART PINCODE RESOLUTION (FIX 🔥)
    # =========================================

    if pincode:
        combined_text = (address or "") + " " + text

        # ✅ use advanced resolver (important)
        pin_state = get_state_from_pincode(pincode, combined_text)

        if pin_state:
            # 1️⃣ If no state → use pincode
            if not state:
                state = pin_state
                state_source = "pincode"

            # 2️⃣ Only fix weak OCR (NOT blindly override)
            elif state_source == "global_text" and state.lower() != pin_state.lower():

                if is_weak_state_match(state, text):
                    state = pin_state
                    state_source = "pincode"    

    return state, pincode






#============================================Aadhar============

def extract_aadhaar_letter_data(text: str) -> Dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    lower_text = text.lower()
    #upper = text.upper()
    upper_text = text.upper()

    id_number = None
    name = None
    dob = None
    address = None

    state = None
    pincode = None

    # 1️⃣ Ensure Aadhaar context exists
    if not any(x in lower_text for x in [
        "aadhaar",
        "unique identification",
        "uidai",
        "government of india"
    ]):
        return {
            "id_number": None,
            "is_valid_id": False,
            "name": None,
            "dob": None,
            "address": None
        }

    # 2️⃣ Masked Aadhaar extraction
    #MASKED_AADHAAR_REGEX = r"[Xx]{4,}\d{4}"
    masked_match = re.search(MASKED_AADHAAR_REGEX, text)
    if masked_match:
        id_number = masked_match.group(0)

    # 3️⃣ Strict DOB extraction (Only if labeled DOB)
    dob_match = re.search(r"DOB[:\s]*" + DATE_REGEX, upper_text)
    if dob_match:
        date_match = DATE_PATTERN.search(dob_match.group(0))
        if date_match:
            dob = date_match.group(0)

    # 4️⃣ Name extraction ONLY for non-back-side Aadhaar
    if not is_aadhaar_back_side(text):
        for i, line in enumerate(lines):
            if RELATION_PATTERN.search(line.upper()):
                if i > 0:
                    candidate = lines[i - 1]
                    lower_candidate = candidate.lower()

                    if any(x in lower_candidate for x in [
                        "authorized",
                        "authentication",
                        "signatory",
                        "government",
                        "authority",
                        "uidai",
                        "aadhaar",
                        "information",
                        "online",
                        "verify"
                    ]):
                        continue

                    cleaned = sanitize_name(candidate)

                    if cleaned and 2 <= len(cleaned.split()) <= 4:
                        name = cleaned
                        break
    

    # 5️⃣ Address extraction (After "Address:")
    for i, line in enumerate(lines):
        lower_line = line.lower()

        #if "address" in lower_line:
        if re.match(r"^\s*address\s*:?", lower_line):    
            address_parts = []

            # keep same-line content after "Address:"
            same_line_part = re.split(r"address\s*:?", line, flags=re.IGNORECASE, maxsplit=1)
            '''if len(same_line_part) > 1:
                part = strip_relation_prefix(same_line_part[1]).strip()
                if part and not should_skip_address_line(part):
                    address_parts.append(part)'''

            if len(same_line_part) > 1:
                part = strip_relation_prefix(same_line_part[1]).strip()
                if part and not is_address_stop_line(part):
                    address_parts.append(part)        
   

            for j in range(i + 1, len(lines)):
                part = lines[j]

                #if should_skip_address_line(part):
                 #   break
                if is_address_stop_line(part):
                    break

                part = strip_relation_prefix(part)

                if len(part.strip()) < 3:
                    continue

                address_parts.append(part)

                if re.search(r"\b\d{6}\b", part):
                    break

            if address_parts:
                address = " ".join(address_parts)
                address = re.sub(r"\s+", " ", address).strip()

            break

    # 5B️⃣ Fallback: address from "To" block if no Address: block
    if not address:
        for i, line in enumerate(lines):
            if line.strip().lower() == "to":
                address_parts = []

                for j in range(i + 1, len(lines)):
                    part = lines[j]

                    # stop when summary/footer starts
                    if DATE_PATTERN.search(part):
                        break

                    '''if any(x in part.lower() for x in [
                        "your aadhaar no", "vid", "government of india",
                        "aadhaar is proof", "download", "verify", "authentication"
                    ]):
                        break'''
                    if is_address_stop_line(part):
                        break

                    part = strip_relation_prefix(part)

                    # skip likely holder-name line only at the very beginning
                    candidate_name = sanitize_name(normalize_name_format(part))
                    if candidate_name and 1 <= len(candidate_name.split()) <= 4 and not DIGIT_PATTERN.search(part):
                        if not address_parts:
                            continue

                    if len(part.strip()) < 3:
                        continue

                    address_parts.append(part)

                    if re.search(r"\b\d{6}\b", part):
                        break

                if address_parts:
                    address = " ".join(address_parts)
                    address = re.sub(r"\s+", " ", address).strip()

                break



    # PRIMARY (from address if exists)

    
    state, pincode = resolve_state_pincode_from_text(text, address)



    return {
        "id_number": id_number,
        "is_valid_id": True if id_number else False,
        "name": name,
        "dob": dob,
        "address": address,
        "state": state,
        "pincode": pincode
    }


def is_full_aadhaar_card(text: str) -> bool:
    text = text.replace('：', ':').replace('／', '/')
    lower = text.lower()

    has_address = bool(re.search(r'address\s*:?', lower))
    has_footer = "www" in lower or "help" in lower or "1947" in lower or "uidai" in lower
    has_gender = "male" in lower or "female" in lower
    has_dob = (
        "dob" in lower
        or "date of birth" in lower
        or "year of birth" in lower
        or bool(DATE_PATTERN.search(text))
    )
    has_number = bool(AADHAAR_PATTERN.search(text) or re.search(MASKED_AADHAAR_REGEX, text))

    has_identity_side = (
        "your aadhaar no" in lower
        or "issue date" in lower
        or "enrolmentno" in lower
        or "enrolment no" in lower
    )

    return has_address and has_number and (
        (has_footer and has_gender and has_dob)
        or (has_footer and has_identity_side)
    )



def is_aadhaar_front_side(text: str) -> bool:
    lower = text.lower()

    has_address = "address" in lower
    has_gender = "male" in lower or "female" in lower or "transgender" in lower
    has_dob = (
        "dob" in lower
        or "d0b" in lower
        or "date of birth" in lower
        or "year of birth" in lower
        or bool(DATE_PATTERN.search(text))
    )
    has_number = bool(AADHAAR_PATTERN.search(text) or re.search(MASKED_AADHAAR_REGEX, text))

    return (not has_address) and has_number and (has_dob or has_gender)


def looks_like_aadhaar_address_fragment(name: str) -> bool:
    if not name:
        return False

    lower = name.lower()

    address_tokens = [
        "village", "district", "dist", "state", "pin", "po", "vtc",
        "road", "street", "colony", "layout", "phase", "market",
        "laldora", "lal dora", "near", "opp", "flat", "house",
        "nagar", "camp", "extension", "extn", "plot", "lane", "apartment",
        "burari", "hebbal"   
    ]

    return any(x in lower for x in address_tokens)


def extract_full_aadhaar_name(lines, id_number: Optional[str], dob: Optional[str]) -> Optional[str]:
    # 1) Prefer "To" block
    for i, line in enumerate(lines):
        if line.strip().lower() == "to":
            for j in range(i + 1, min(i + 5, len(lines))):
                candidate_line = lines[j].strip()
                lower_candidate = candidate_line.lower()

                if not candidate_line:
                    continue

                if is_relation_line(candidate_line):
                    break

                if DIGIT_PATTERN.search(candidate_line):
                    continue

                if any(x in lower_candidate for x in [
                    "village", "district", "state", "pin", "po:", "vtc:",
                    "road", "street", "colony", "layout", "phase", "market",
                    "laldora", "lal dora", "near", "opp", "flat", "house",
                    "nagar", "camp", "extension", "extn", "plot", "lane", "apartment",
                    "government", "uidai", "aadhaar", "enrolment", "information"
                ]):
                    continue

                candidate = sanitize_name(normalize_name_format(candidate_line))
                if candidate and 1 <= len(candidate.split()) <= 4 and not looks_like_aadhaar_address_fragment(candidate):
                    return candidate
            break

    # 2) Fallback near DOB/gender
    for i, line in enumerate(lines):
        lower_line = line.lower()

        if (dob and dob in line) or "male" in lower_line or "female" in lower_line:
            for offset in [1, 2]:
                if i - offset >= 0:
                    candidate = sanitize_name(normalize_name_format(lines[i - offset]))
                    if candidate and 1 <= len(candidate.split()) <= 4 and not looks_like_aadhaar_address_fragment(candidate):
                        return candidate
            break

    # 3) Final generic fallback
    candidate = extract_name_smart("\n".join(lines), id_number)
    if candidate and not looks_like_aadhaar_address_fragment(candidate):
        return candidate

    return None





def extract_aadhaar_data(text: str) -> Dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    id_number = extract_regex(text, AADHAAR_PATTERN)

    dob_match = re.search(
        r"(dob|d0b)[^\d]*(\d{1,2}[/-]\d{1,2}[/-]\d{4})",
        text,
        re.IGNORECASE
    )
    if dob_match:
        dob = dob_match.group(2)
    else:
        dob = extract_dob_from_dates(text)

    gender = extract_gender(text)
    phone = extract_phone(text)

    full_card = is_full_aadhaar_card(text)
    front_side = is_aadhaar_front_side(text)
    back_side = is_aadhaar_back_side(text)

    # FULL CARD
    if full_card:
        extracted_name = extract_full_aadhaar_name(lines, id_number, dob)

        if extracted_name and looks_like_aadhaar_address_fragment(extracted_name):
            extracted_name = None

        extracted_address = extract_aadhaar_address(text)

        if extracted_address:
            extracted_address = re.sub(r"\s+", " ", extracted_address).strip()

        state, pincode = resolve_state_pincode_from_text(text, extracted_address)

        return {
            "id_number": id_number,
            #"is_valid_id": bool(id_number),
            "is_valid_id": True if id_number else False,
            "name": extracted_name,
            "dob": dob,
            "gender": gender,
            "phone": phone,
            "address": extracted_address,
            "state": state,
            "pincode": pincode
        }

    # FRONT SIDE
    
    if front_side:
        extracted_name = extract_name_smart(text, id_number)
        if extracted_name and looks_like_aadhaar_address_fragment(extracted_name):
            extracted_name = None

        state, pincode = resolve_state_pincode_from_text(text, None)

        return {
            "id_number": id_number,
            #"is_valid_id": bool(id_number),
            "is_valid_id": True if id_number else False,
            "name": extracted_name,
            "dob": dob,
            "gender": gender,
            "phone": phone,
            "address": None,
            "state": state,
            "pincode": pincode
        }
    
    

    # AMBIGUOUS IDENTITY SIDE
    if not back_side:
        extracted_name = extract_name_smart(text, id_number)
        if extracted_name and looks_like_aadhaar_address_fragment(extracted_name):
            extracted_name = None

        state, pincode = resolve_state_pincode_from_text(text, None)

        return {
            "id_number": id_number,
            #"is_valid_id": bool(id_number),
            "is_valid_id": True if id_number else False,
            "name": extracted_name,
            "dob": dob,
            "gender": gender,
            "phone": phone,
            "address": None,
            "state": state,
            "pincode": pincode
        }


    # BACK SIDE
    if back_side:
        address = extract_aadhaar_address(text)
    else:
        address = None
    
    state, pincode = resolve_state_pincode_from_text(text, address)

    return {
        "id_number": id_number,
        #"is_valid_id": bool(id_number),
        "is_valid_id": True if id_number else False,
        "name": None,
        "dob": None if back_side else dob,
        "gender": None if back_side else gender,
        "phone": None if back_side else phone,
        "address": address,
        "state": state,
        "pincode": pincode
    }





def extract_aadhaar_address(text: str) -> Optional[str]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    address_lines = []

    # 1️⃣ Try "Address:" block
    capture = False
    for line in lines:
        lower = line.lower()

        #if "address" in lower:
        if re.match(r"^\s*address\s*:?", lower):    
            capture = True

            # same-line content after Address:
            same_line_part = re.split(r"address\s*:?", line, flags=re.IGNORECASE, maxsplit=1)
            if len(same_line_part) > 1:
                part = strip_relation_prefix(same_line_part[1]).strip()
                #if part and not should_skip_address_line(part):
                 #   address_lines.append(part)
                if part and not is_address_stop_line(part):
                    address_lines.append(part)     

            continue

        if capture:
            #if should_skip_address_line(line):
                #break
            if is_address_stop_line(line):
                break

            if re.search(r"\b(help@|www\.|uidai\.gov|1947)\b", line.lower()):
                break

            part = strip_relation_prefix(line)

            if len(part.strip()) < 3:
                continue

            address_lines.append(part)

            if re.search(r"\b\d{6}\b", part):
                break

    # 2️⃣ FALLBACK: "To" block
    if not address_lines:
        for i, line in enumerate(lines):
            if line.lower() == "to":
                for j in range(i + 1, len(lines)):
                    part = lines[j]

                    #if should_skip_address_line(part):
                     #   break
                    if is_address_stop_line(part):
                        break

                    if re.search(r"\b(help@|www\.|uidai\.gov|1947)\b", part.lower()):
                        break

                    part = strip_relation_prefix(part)

                    # Skip likely holder name at the beginning of To block
                    candidate_name = sanitize_name(normalize_name_format(part))
                    if candidate_name and 1 <= len(candidate_name.split()) <= 4 and not DIGIT_PATTERN.search(part):
                        if not address_lines:
                            continue

                    if len(part.strip()) < 3:
                        continue

                    address_lines.append(part)

                    if re.search(r"\b\d{6}\b", part):
                        break
                break

    # 3️⃣ FALLBACK: PIN-based
    if not address_lines:
        if any(x in text.lower() for x in ["road", "street", "colony", "nagar", "district", "state", "pin"]):
            for i, line in enumerate(lines):
                if re.search(r"\b\d{6}\b", line):
                    start = max(0, i - 6)
                    candidate_block = []
                    for x in lines[start:i + 1]:
                        #if should_skip_address_line(x):
                         #   continue
                        if is_address_stop_line(x):
                            continue
                        part = strip_relation_prefix(x)
                        if len(part.strip()) < 3:
                            continue
                        candidate_block.append(part)
                    if candidate_block:
                        address_lines = candidate_block
                        break

    if not address_lines:
        return None

    full_address = " ".join(address_lines).strip()
    full_address = re.sub(r"\s+", " ", full_address)

    alpha_words = re.findall(r"[A-Za-z]{3,}", full_address)
    if len(alpha_words) < 2:
        return None

    if len(full_address) < 10:
        return None

    return full_address



PINCODE_PREFIX_CANDIDATES = {
    "74": ["West Bengal", "Andaman and Nicobar Islands"],
    "20": ["Uttar Pradesh", "Uttarakhand"],
    "26": ["Uttar Pradesh", "Uttarakhand"],
    "80": ["Bihar", "Jharkhand"],
    "50": ["Telangana", "Andhra Pradesh"],
    "36": ["Gujarat", "Dadra and Nagar Haveli and Daman and Diu"],
    "40": ["Maharashtra", "Goa"],
}

PINCODE_PREFIX_CANDIDATES = {
    "74": ["West Bengal", "Andaman and Nicobar Islands"],
    "20": ["Uttar Pradesh", "Uttarakhand"],
    "24": ["Uttar Pradesh", "Uttarakhand"],  # ADD THIS
    "26": ["Uttar Pradesh", "Uttarakhand"],
    "80": ["Bihar", "Jharkhand"],
    "81": ["Bihar", "Jharkhand"],  # ADD
    "82": ["Jharkhand", "Bihar"],  # ADD
    "50": ["Telangana", "Andhra Pradesh"],
    "36": ["Gujarat", "Dadra and Nagar Haveli and Daman and Diu"],
    "40": ["Maharashtra", "Goa"],
}

PINCODE_STATE_MAP_3 = {
    

    # UTs that conflict with State prefixes
    "160": "Chandigarh",
    "194": "Ladakh",
    "605": "Puducherry",
    

    # North Eastern state distinctions
    "790": "Arunachal Pradesh", "791": "Arunachal Pradesh", "792": "Arunachal Pradesh",
    "793": "Meghalaya", "794": "Meghalaya",
    "795": "Manipur",
    "796": "Mizoram",
    "797": "Nagaland", "798": "Nagaland",
    "799": "Tripura",

    # Add more only when you observe real ambiguity in OCR production

    # Goa vs Maharashtra
    "403": "Goa",

    # Uttarakhand vs UP (extend)
    "244": "Uttarakhand", "245": "Uttarakhand", "246": "Uttarakhand",
    "247": "Uttarakhand", "248": "Uttarakhand", "249": "Uttarakhand",
    "262": "Uttarakhand", "263": "Uttarakhand",

    # West Bengal vs Islands
    "737": "Sikkim",
    "744": "Andaman and Nicobar Islands",

    # Kerala vs Lakshadweep
    "682": "Lakshadweep",

    # Gujarat vs DNHDD
    "396": "Dadra and Nagar Haveli and Daman and Diu",




    # Bihar vs Jharkhand (CRITICAL FIX)
    "800": "Bihar", "801": "Bihar", "802": "Bihar", "803": "Bihar",
    "804": "Bihar", "805": "Bihar", "806": "Bihar", "807": "Bihar",
    "808": "Bihar", "809": "Bihar", "810": "Jharkhand",
    "811": "Jharkhand", "812": "Jharkhand", "813": "Jharkhand",
    "814": "Jharkhand", "815": "Jharkhand", "816": "Jharkhand",
    "817": "Jharkhand", "818": "Jharkhand", "819": "Jharkhand",
    "820": "Jharkhand", "821": "Jharkhand", "822": "Jharkhand",
    "823": "Jharkhand", "824": "Jharkhand", "825": "Jharkhand",
    "826": "Jharkhand", "827": "Jharkhand", "828": "Jharkhand",
    "829": "Jharkhand", "830": "Jharkhand", "831": "Jharkhand",
    "832": "Jharkhand", "833": "Jharkhand", "834": "Jharkhand",
    "835": "Jharkhand",
}



PINCODE_STATE_MAP_2 = {
    "11": "Delhi",

    "12": "Haryana", "13": "Haryana",

    "14": "Punjab", "15": "Punjab", "16": "Chandigarh",

    "17": "Himachal Pradesh",

    "18": "Jammu and Kashmir", "19": "Jammu and Kashmir",

    "20": "Uttar Pradesh", "21": "Uttar Pradesh", "22": "Uttar Pradesh",
    "23": "Uttar Pradesh", "24": "Uttar Pradesh", "25": "Uttar Pradesh",
    "26": "Uttarakhand",

    "30": "Rajasthan", "31": "Rajasthan", "32": "Rajasthan",

    "33": "Gujarat", "34": "Gujarat", "36": "Gujarat", "39": "Gujarat",

    "40": "Maharashtra", "41": "Maharashtra", "42": "Maharashtra",
    "43": "Maharashtra", "44": "Maharashtra",

    "45": "Madhya Pradesh", "46": "Madhya Pradesh", "47": "Madhya Pradesh",
    "48": "Chhattisgarh", "49": "Chhattisgarh",

    "50": "Telangana",
    "51": "Andhra Pradesh", "52": "Andhra Pradesh", "53": "Andhra Pradesh",

    "56": "Karnataka", "57": "Karnataka", "58": "Karnataka",

    "60": "Tamil Nadu", "61": "Tamil Nadu", "62": "Tamil Nadu",
    "63": "Tamil Nadu", "64": "Tamil Nadu",

    "67": "Kerala", "68": "Kerala", "69": "Kerala",

    "70": "West Bengal", "71": "West Bengal", "72": "West Bengal",
    "73": "West Bengal", "74": "West Bengal",

    "75": "Odisha", "76": "Odisha",

    "78": "Assam", "79": "Arunachal Pradesh",

    "80": "Bihar", "81": "Bihar",
    "82": "Jharkhand", "83": "Jharkhand",
}




STATE_ALIASES = {
    "odisha": "Odisha",
    "orissa": "Odisha",
    "uttaranchal": "Uttarakhand",
    "uttrakhand": "Uttarakhand",
    "uttarakhand": "Uttarakhand",
    "pondicherry": "Puducherry",
    "nct of delhi": "Delhi",
    "j&k": "Jammu And Kashmir",
}



#def get_state_from_pincode_advanced(pincode: str, text: str = ""):
def get_state_from_pincode(pincode: str, text: str = ""):
    if not pincode or len(pincode) != 6:
        return None

    # 1️⃣ Exact 3-digit override (strong)
    state = PINCODE_STATE_MAP_3.get(pincode[:3])
    if state:
        return state

    # 2️⃣ Candidate states (ambiguous zones)
    candidates = PINCODE_PREFIX_CANDIDATES.get(pincode[:2])

    if candidates:
        text_lower = text.lower()

        # ✅ resolve using text
        for s in candidates:
            if s.lower() in text_lower:
                return s

        # ❌ do NOT guess
        #return None
        return candidates[0]  # fallback

    # 3️⃣ fallback (only if no ambiguity)
    return PINCODE_STATE_MAP_2.get(pincode[:2])



'''def get_state_from_pincode(pincode: Optional[str]) -> Optional[str]:
    if not pincode or len(pincode) != 6 or not pincode.isdigit():
        return None

    # specific first
    state = PINCODE_STATE_MAP_3.get(pincode[:3])
    if state:
        return state

    # broad fallback
    return PINCODE_STATE_MAP_2.get(pincode[:2])
'''



def extract_address_components(address: str, full_text: str = None):
    if not address:
        return None, None

    address = re.sub(r"([a-zA-Z])(\d{6})", r"\1 \2", address)
    address = re.sub(r"(\d{6})([a-zA-Z])", r"\1 \2", address)

    # pincode from address only
    candidates = re.findall(r"\b\d{6}\b", address)
    valid_pins = []

    for pin in candidates:
        if pin == "000000":
            continue
        if full_text and is_part_of_aadhaar(pin, full_text):
            continue
        valid_pins.append(pin)

    pincode = valid_pins[-1] if valid_pins else None

    # state from address only
    state = None
    lower_addr = address.lower()

    for s in INDIA_STATES:
        pattern = r"(?<![a-zA-Z])" + re.escape(s).replace(r"\ ", r"\s+") + r"(?![a-zA-Z])"
        if re.search(pattern, lower_addr, re.IGNORECASE):
            state = STATE_ALIASES.get(s, s.title())
            break

    # joined OCR fallback inside address only
    if not state:
        compact = re.sub(r"\s+", "", address).lower()
        for s in INDIA_STATES:
            key = s.replace(" ", "").lower()
            pattern = re.escape(key) + r"(?=(?:[-,:/ ]?\d{6})|[-,:/ ]|$)"
            if re.search(pattern, compact, re.IGNORECASE):
                state = STATE_ALIASES.get(s, s.title())
                break

    #return state, pincode

    # FINAL STATE RESOLUTION (FIX 🔥)
    # =========================================

    final_state = state

    if pincode:
        combined_text = (address or "") + " " + (full_text or "")

        pin_state = get_state_from_pincode(pincode, combined_text)

        # ✅ ONLY fallback (DO NOT override)
        if not final_state:
            final_state = pin_state

    return final_state, pincode


def is_aadhaar_back_side(text: str) -> bool:
    text = text.replace('：', ':').replace('／', '/')
    lower = text.lower()

    # Full card should never be treated as back side
    if is_full_aadhaar_card(text):
        return False

    has_address = bool(re.search(r'address\s*:?', lower))
    has_uidai = "uidai" in lower or "unique identification authority" in lower
    has_footer = "www" in lower or "help" in lower or "1947" in lower

    has_gender = "male" in lower or "female" in lower
    has_dob = "dob" in lower or "date of birth" in lower or "year of birth" in lower

    has_relation = bool(re.search(r"\b(c|s|d|w)\s*/?\s*o\b", lower))

    if has_address and (has_uidai or has_footer):
        return True

    if has_address and has_relation:
        return True

    if has_address and not (has_gender and has_dob):
        return True

    return False





def extract_nepal_citizenship(text: str) -> Dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    upper = text.upper()

    # Citizenship number
    id_match = re.search(r"\d{2}-\d{2}-\d{2}-\d{5}", text)
    id_number = id_match.group(0) if id_match else None

    name = None

    #  Rule 1: Line after "Full Name"
    for i, line in enumerate(lines):
        if "FULL NAME" in line.upper():
            if i + 1 < len(lines):
                candidate = sanitize_name(lines[i + 1])
                if candidate:
                    name = candidate
                    break

    #  Rule 2: Regex fallback (same line case)
    if not name:
        name_match = re.search(r"FULL NAME[:\s]*([A-Z ]{3,})", upper)
        if name_match:
            candidate = sanitize_name(name_match.group(1))
            if candidate:
                name = candidate

    # DOB extraction
    year_match = re.search(r"YEAR[:\s]*(\d{4})", upper)
    day_match = re.search(r"DAY[:\s]*(\d{1,2})", upper)

    dob = None
    if year_match and day_match:
        dob = f"{day_match.group(1)}/01/{year_match.group(1)}"

    return {
        "id_number": id_number,
        #"is_valid_id": bool(id_number),
        "is_valid_id": True if id_number else False,
        "name": name,
        "dob": dob,
        "address": None
    }

def extract_generic_id(text: str) -> Dict:
    id_match = re.search(r"\b\d{6,}\b", text)
    id_number = id_match.group(0) if id_match else None

    dob = extract_dob_from_dates(text)
    name = extract_name_smart(text, id_number)

    return {
        "id_number": id_number,
        "is_valid_id": True if id_number else False,
        "name": name,
        "dob": dob,
        "address": None
    }




def compute_id_confidence(text: str, id_number: Optional[str]) -> float:
    if not id_number:
        return 0.0

    if PAN_PATTERN.search(text.upper()):
        return 0.99

    if DL_PATTERN.search(text.upper()):
        return 0.98

    if VOTER_PATTERN.search(text.upper()):
        return 0.97

    if AADHAAR_PATTERN.search(text):
        return 0.99

    # Generic fallback
    if len(id_number) >= 6:
        return 0.85

    return 0.5


LABEL_NOISE_WORDS = [
    "name", "fathername", "fathersname", "father s name",
    "address", "addess", "adress", "addres",
    "dateofbirth", "dob", "male", "female", "transgender",
    "government", "governmentofindia", "uidai", "aadhaar",
    "pancard", "card", "signature", "permanentaccountnumber",
    "incometaxdepartment", "elector", "sex"
]

def looks_like_label_noise(name: str) -> bool:
    if not name:
        return True

    n = normalize_name_format(name).lower().replace(" ", "").replace("/", "")

    # exact or contained matches
    if any(word.replace(" ", "") in n for word in LABEL_NOISE_WORDS):
        return True

    # fuzzy OCR mistakes like Addess ~ Address
    for word in LABEL_NOISE_WORDS:
        ratio = difflib.SequenceMatcher(None, n, word.replace(" ", "")).ratio()
        if ratio >= 0.82:
            return True

    return False


'''def compute_name_confidence(name: Optional[str]) -> float:
    if not name:
        return 0.0


    # Normalize first
    name = normalize_name_format(name)

    # Rule-Based Confidence
    score = 0
    words = name.split()

    # Word count
    if 2 <= len(words) <= 4:
        score += 3
    #elif len(words) == 1:
     #   score += 1
    elif len(words) == 1 and len(words[0]) >= 4:
        score += 2    
    else:
        score -= 2


    valid_words = sum(1 for w in words if len(w) >= 2)



    if valid_words == len(words):
        score += 3
    else:
        score -= 2

    # Alphabet only
    if name.replace(" ", "").isalpha():
        score += 2
    else:
        score -= 3


    if name.istitle():
        score += 2
    elif name.isupper() and len(name.split()) >= 2:
        score += 1




    rule_confidence = max(min(score / 10, 1.0), 0.0)

    # NER Validation
    ner_confidence = validate_name_with_ner(name)

    # FINAL PRODUCTION LOGIC

    #if NER_ENABLED:

        # If rule already strong → don't let NER reduce it
        if rule_confidence >= 0.75:
            final_confidence = max(rule_confidence, ner_confidence)

        # If rule weak → combine both
        else:
            final_confidence = (rule_confidence * 0.5) + (ner_confidence * 0.5)

    else:
        final_confidence = rule_confidence
        
    #return round(final_confidence, 2)


    #if NER_ENABLED:

        if ner_confidence < 0.3:
            final_confidence = rule_confidence * 0.4
        else:
            final_confidence = (rule_confidence * 0.6) + (ner_confidence * 0.4)

    else:
        final_confidence = rule_confidence    

    #return round(final_confidence, 2)


    if NER_ENABLED:

        # If rule confidence already strong, trust it
        #if rule_confidence >= 0.8:
        #    final_confidence = rule_confidence

        if rule_confidence >= 0.85 and ner_confidence > 0.6:
            final_confidence = rule_confidence    

        # If rule weak, then consult NER
        elif ner_confidence < 0.3:
            #final_confidence = rule_confidence * 0.5
            final_confidence = rule_confidence * 0.7

        else:
            final_confidence = (rule_confidence * 0.6) + (ner_confidence * 0.4)

    else:
        final_confidence = rule_confidence

    return round(final_confidence, 2)


    #if NER_ENABLED:

        # Strong rule extraction
        if rule_confidence >= 0.85:
            final_confidence = rule_confidence

        # Weak NER -> penalize
        elif ner_confidence < 0.3:
            final_confidence = rule_confidence * 0.4

        # Combine
        else:
            final_confidence = (rule_confidence * 0.5) + (ner_confidence * 0.5)

    else:
        final_confidence = rule_confidence

    #return round(final_confidence, 2) '''   




def compute_name_confidence(name: Optional[str]) -> float:
    if not name:
        return 0.0

    # normalize
    name = normalize_name_format(name)

    # sanitize first
    cleaned = sanitize_name(name)
    if not cleaned:
        return 0.0

    name = cleaned

    # hard reject label / OCR junk
    if looks_like_label_noise(name):
        return 0.0

    words = name.split()
    score = 0.0

    # =========================
    # RULE SCORE
    # =========================

    # Word count
    if 2 <= len(words) <= 4:
        score += 0.45
    elif len(words) == 1:
        # single-word names are possible, but weaker
        if len(words[0]) >= 5:
            score += 0.15
        else:
            score -= 0.10
    elif len(words) == 5:
        score += 0.20
    else:
        score -= 0.25

    # Valid word lengths
    valid_words = sum(1 for w in words if len(w) >= 2)
    if valid_words == len(words):
        score += 0.20
    else:
        score -= 0.20

    # Alphabet only
    if name.replace(" ", "").isalpha():
        score += 0.20
    else:
        score -= 0.30

    # Title case / uppercase handling
    if name.istitle():
        score += 0.15
    elif name.isupper() and len(words) >= 2:
        score += 0.05

    # Penalty for very generic single word
    if len(words) == 1:
        score -= 0.15

    # Penalty for long merged OCR word like Rajeshbhripatiyadav
    if len(words) == 1 and len(words[0]) > 12:
        score -= 0.10

    rule_confidence = max(min(score, 1.0), 0.0)

    # =========================
    # NER SCORE
    # =========================
    ner_confidence = validate_name_with_ner(name)

    # =========================
    # FINAL GATING LOGIC
    # =========================

    # Single-word names must have stronger NER to be trusted
    if len(words) == 1:
        if ner_confidence < 0.80:
            final_confidence = min(rule_confidence, 0.55)
        else:
            final_confidence = (rule_confidence * 0.50) + (ner_confidence * 0.50)

    else:
        # If NER is very weak, cap strongly
        if ner_confidence < 0.35:
            final_confidence = min(rule_confidence * 0.60, 0.65)

        # If NER is moderate, do not allow very high confidence
        elif ner_confidence < 0.60:
            final_confidence = min((rule_confidence * 0.70) + (ner_confidence * 0.30), 0.79)

        # Strong NER + strong rule can go high
        else:
            final_confidence = (rule_confidence * 0.55) + (ner_confidence * 0.45)

    # Final guarantee:
    # confidence > 0.8 only if both rule and NER are strong
    if not (rule_confidence >= 0.75 and ner_confidence >= 0.75):
        final_confidence = min(final_confidence, 0.79)

    return round(max(min(final_confidence, 1.0), 0.0), 2)



def validate_name_with_ner(name: str) -> float:

    if not NER_ENABLED or not name:
        return 0.0
    
    if looks_like_label_noise(name):
        return 0.0

    try:
        formatted_name = normalize_name_format(name)

        result = ner(formatted_name)

        if not result:
            return 0.0

        scores = []

        for ent in result:

            label = ent.get("entity_group", "")

            if label in ["PER", "PERSON"]:

                scores.append(ent.get("score", 0))

        if not scores:
            return 0.0

        return round(sum(scores) / len(scores), 2)

    except Exception:
        return 0.0





def evaluate_batch(results: list) -> Dict:
    total = len(results)
    if total == 0:
        return {"pass_percentage": 0}

    passed = 0

    for r in results:
        if (
            # ✅ Safe way: Use (r.get("id_confidence") or 0)
            (r.get("id_confidence") or 0) > 0.9 and 
            (r.get("name_confidence") or 0) > 0.6
        ):
            passed += 1

    percentage = round((passed / total) * 100, 2)

    return {
        "total_documents": total,
        "passed_documents": passed,
        "pass_percentage": percentage
    }



# MAIN ENTRY
# =====================================================

'''def extract_structured_data(text: str, image_path: str = None, images=None):
    import time

    pipeline_start = time.time()

    text = clean_text(text)
    doc_type = detect_document_type(text)

    result = {
        "document_type": doc_type if doc_type else "unknown",
        "name": None,
        "id_number": None,
        "dob": None,
        "address": None,
        "is_valid_id": False,
    }

    if not doc_type:
        return result

    # =====================================================
    # DOCUMENT-SPECIFIC EXTRACTION
    # =====================================================

    if doc_type == "driving_license":
        data = extract_dl_data(text)

    elif doc_type == "pancard":
        data = extract_pan_data(text)

    elif doc_type == "aadhaar":
        full_aadhaar = is_full_aadhaar_card(text)
        aadhaar_back = is_aadhaar_back_side(text)

        print(f"[AADHAAR SIDE] full_card={full_aadhaar} back_side={aadhaar_back}")

        data = extract_aadhaar_data(text)

        if aadhaar_back and not full_aadhaar:
            data["name"] = None
            data["name_confidence"] = 0.0

            if (
                not data.get("id_number")
                or not data.get("address")
                or not data.get("state")
                or not data.get("pincode")
            ):
                letter_data = extract_aadhaar_letter_data(text)

                for key in ["id_number", "dob", "address", "state", "pincode", "phone", "gender"]:
                    if not data.get(key):
                        data[key] = letter_data.get(key)

                if data.get("id_number"):
                    data["is_valid_id"] = True

        else:
            if not data.get("id_number") or not data.get("name"):
                letter_data = extract_aadhaar_letter_data(text)

                for key in ["id_number", "name", "dob", "address", "state", "pincode", "phone", "gender"]:
                    if not data.get(key):
                        data[key] = letter_data.get(key)

                if data.get("id_number"):
                    data["is_valid_id"] = True

    elif doc_type == "voter_id":
        data = extract_voter_strong(text)

    elif doc_type == "nepal_citizenship":
        data = extract_nepal_citizenship(text)

    elif doc_type in ["student_id", "work_id"]:
        data = extract_generic_id(text)

    else:
        return result

    # =====================================================
    # MERGE EXTRACTED DATA
    # =====================================================

    result.update(data)

    def cleanup_internal_fields():
        result.pop("needs_qwen_name", None)
        result.pop("pan_name_candidates", None)
        result.pop("pan_rule_fallback_name", None)

    def finalize_result():
        if result.get("name_confidence") is not None:
            result["name_confidence"] = float(result["name_confidence"])

        if result.get("id_confidence") is not None:
            result["id_confidence"] = float(result["id_confidence"])

        if result.get("clip_score") is not None:
            result["clip_score"] = float(result["clip_score"])

        cleanup_internal_fields()
        return result

    # Normalize extracted name (fix CamelCase OCR issues)
    # -----------------------------------------------------
    if result.get("name"):
        result["name"] = normalize_name_format(result["name"])
        result["name"] = sanitize_name(result["name"])

    # Aadhaar-specific safety:
    # if extracted "name" looks like address text, remove it BEFORE confidence is computed
    if doc_type == "aadhaar" and result.get("name"):
        if looks_like_aadhaar_address_fragment(result["name"]):
            print(f"[AADHAAR NAME RESET] Removed address-like name: {result['name']}")
            result["name"] = None

    # =====================================================
    # ID CONFIDENCE
    # =====================================================

    result["id_confidence"] = compute_id_confidence(
        text,
        result.get("id_number")
    )

    # =====================================================
    # NAME CONFIDENCE
    # =====================================================

    result["name_confidence"] = compute_name_confidence(
        result.get("name")
    )

    name = result.get("name")
    name_conf = result.get("name_confidence", 0)

    #forced_pan_qwen = (
     #   doc_type == "pancard" and bool(result.get("needs_qwen_name"))
    #)
    forced_pan_qwen = (doc_type == "pancard")
    pan_rule_fallback_name = result.get("pan_rule_fallback_name")
    pan_name_candidates = result.get("pan_name_candidates", []) or []

    # Aadhaar back side: keep name empty, do not run QWEN
    if doc_type == "aadhaar" and is_aadhaar_back_side(text) and not is_full_aadhaar_card(text):
        result["name"] = None
        result["name_confidence"] = 0.0
        return finalize_result()

    print(f"[FALLBACK CHECK] name={name} conf={name_conf}")

    # =====================================================
    # QWEN FALLBACK (FINAL PRODUCTION VERSION 🚀)
    # =====================================================

    def needs_qwen_fallback(name, conf):
        """
        Decide whether Qwen is needed
        """

        if not name:
            return True

        if conf is None:
            return True

        if conf < 0.8:
            return True

        if not re.fullmatch(r"[A-Za-z ]+", name):
            return True

        if any(x in name.lower() for x in [
            "wifi", "signature", "govt", "income", "tax", "department", "pan", "card",
            "government", "india", "name"
        ]):
            return True

        return False

    generic_need_qwen = needs_qwen_fallback(name, name_conf)
    final_need_qwen = forced_pan_qwen or generic_need_qwen

    print(f"[FALLBACK DECISION] generic={generic_need_qwen} forced_pan={forced_pan_qwen} final={final_need_qwen}")

    if not final_need_qwen:
        print(f"[TIME] TOTAL PIPELINE: {round(time.time() - pipeline_start, 3)} sec")
        return finalize_result()

    # ENSURE IMAGE EXISTS
    #if not image_path:
     #   print(f"[TIME] TOTAL PIPELINE: {round(time.time() - pipeline_start, 3)} sec")
      #  return finalize_result()


    # =====================================================
    # ENSURE SOME VISUAL INPUT EXISTS
    # =====================================================

    has_visual_input = bool(image_path) or (images is not None and len(images) > 0)

    if not has_visual_input:
        # ✅ PAN-only safe fallback even when no image path exists
        if doc_type == "pancard" and pan_rule_fallback_name:
            fallback_conf = compute_name_confidence(pan_rule_fallback_name)

            # optional floor/cap so fallback confidence stays reasonable
            fallback_conf = max(fallback_conf, 0.72)
            fallback_conf = min(fallback_conf, 0.88)

            print(f"[PAN FALLBACK] Using OCR fallback without image: {pan_rule_fallback_name} | Conf: {fallback_conf}")
            result.update({
                "name": pan_rule_fallback_name,
                "name_confidence": fallback_conf
            })

        print(f"[TIME] TOTAL PIPELINE: {round(time.time() - pipeline_start, 3)} sec")
        return finalize_result()




    # =====================================================
    # RUN QWEN (PDF + IMAGE SAFE 🚀) with time calculation
    # =====================================================

    print(f"[QWEN] Triggered for: {name} ({name_conf})")

    qwen_start_total = time.time()

    # =========================
    # PDF → IMAGE CONVERSION
    # =========================
    convert_start = time.time()

    qwen_input_path = image_path

    if images is not None and len(images) > 0:
        try:
            import tempfile
            from PIL import Image

            tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            #Image.fromarray(images[0]).save(tmp_file.name, format="JPEG", quality=70)
            Image.fromarray(images[0]).save(tmp_file.name, format="JPEG", quality=95)

            qwen_input_path = tmp_file.name
            print(f"[TIME] PDF->IMAGE: {round(time.time() - convert_start, 3)} sec")

        except Exception as e:
            print(f"[QWEN] Conversion failed: {e}")
            qwen_input_path = image_path

    # =========================
    # QWEN CALL
    # =========================
    qwen_call_start = time.time()

    try:
        qwen_prompt = None


        if doc_type == "pancard" and forced_pan_qwen:
            candidate_hint = ""
            if pan_name_candidates:
                candidate_hint = (
                    "\nPossible OCR candidate names (choose only if visually correct): "
                    + ", ".join(pan_name_candidates[:3])
                    + "\n"
                )

            qwen_prompt = (
                "This is an Indian PAN card.\n"
                "Extract ONLY the PAN HOLDER'S name.\n\n"
                "Important PAN rules:\n"
                "- A PAN card may contain the holder name and the father's name.\n"
                "- Return ONLY the holder name.\n"
                "- Do NOT return the father's name.\n"
                "- Do NOT return DOB.\n"
                "- Do NOT return PAN number.\n"
                "- Do NOT return labels like Name, Father's Name, Signature.\n"
                "- Do NOT return any explanation.\n\n"
                "How to choose the holder name:\n"
                "- Prefer the name nearest the '/Name' label.\n"
                "- If '/Name' is unclear, prefer the name nearest DOB or PAN number.\n"
                "- If two human names are visible, choose the most likely PAN holder name.\n"
                "- Use the father's name only if it is the only visible person name.\n"
                "- Do not return NULL if at least one clear human name is visible.\n\n"
                "Output rules:\n"
                "- Return only one name.\n"
                "- No labels.\n"
                "- No extra words.\n"
                f"{candidate_hint}"
            )



        qwen_name = extract_name_with_qwen(qwen_input_path, prompt=qwen_prompt)

    except Exception as e:
        print(f"[QWEN] Failed: {e}")
        qwen_name = None

    print(f"[TIME] QWEN CALL: {round(time.time() - qwen_call_start, 3)} sec")
    print(f"[QWEN] Raw Output: {qwen_name}")

    # =========================
    # POST PROCESS
    # =========================
    post_start = time.time()

    if qwen_name:
        qwen_name = normalize_name_format(qwen_name).strip()
        qwen_name = sanitize_name(qwen_name)

        if not qwen_name:
            print("[QWEN REJECT] sanitize_name returned None")
            qwen_name = None

        elif not re.fullmatch(r"[A-Za-z ]+", qwen_name):
            print(f"[QWEN REJECT] non alphabetic output: {qwen_name}")
            qwen_name = None

        elif len(qwen_name.split()) > 6:
            print(f"[QWEN REJECT] too many words: {qwen_name}")
            qwen_name = None

        elif len(qwen_name.split()) == 1 and len(qwen_name) < 4:
            print(f"[QWEN REJECT] single short word: {qwen_name}")
            qwen_name = None

        elif any(x in qwen_name.lower() for x in [
            "government", "india", "department", "card",
            "s/o", "d/o", "w/o", "late"
        ]):
            print(f"[QWEN REJECT] blocked word found: {qwen_name}")
            qwen_name = None

    print(f"[TIME] POST PROCESS: {round(time.time() - post_start, 3)} sec")

    # =========================
    # APPLY RESULT
    # =========================

    
    



    if qwen_name:
        print(f"[QWEN] Final: {qwen_name} | Conf: 0.99")
        result.update({
            "name": qwen_name,
            "name_confidence": 0.99
        })
    else:
        # ✅ PAN-only safe fallback when Qwen returns NULL
        if doc_type == "pancard" and pan_rule_fallback_name:
            fallback_conf = compute_name_confidence(pan_rule_fallback_name)

            # optional floor/cap so fallback confidence stays reasonable
            fallback_conf = max(fallback_conf, 0.72)
            fallback_conf = min(fallback_conf, 0.88)

            print(f"[PAN FALLBACK] Using OCR fallback: {pan_rule_fallback_name} | Conf: {fallback_conf}")
            result.update({
                "name": pan_rule_fallback_name,
                "name_confidence": fallback_conf
        })
        else:
            print("[QWEN] Rejected output")




    # =========================
    # CLEANUP
    # =========================
    if qwen_input_path != image_path:
        import os
        try:
            os.remove(qwen_input_path)
        except:
            pass

    print(f"[TIME] TOTAL QWEN BLOCK: {round(time.time() - qwen_start_total, 3)} sec")

    # FINAL SAFETY RULE (PREVENT GARBAGE NAMES)
    if result.get("name_confidence", 0) < 0.3 and result.get("name"):
        result["name_confidence"] = round(
            min(result["name_confidence"], 0.2), 2
        )

    print(f"[TIME] TOTAL PIPELINE: {round(time.time() - pipeline_start, 3)} sec")
    return finalize_result()'''

























# MAIN ENTRY
# =====================================================


def extract_structured_data(text: str, image_path: str = None, images=None):


    import time

    pipeline_start = time.time()


    text = clean_text(text)
    doc_type = detect_document_type(text)

    result = {
        "document_type": doc_type if doc_type else "unknown",
        "name": None,
        "id_number": None,
        "dob": None,
        "address": None,
        "is_valid_id": False,
    }

    if not doc_type:
        return result


    # =====================================================
    # DOCUMENT-SPECIFIC EXTRACTION
    # =====================================================
    


    if doc_type == "driving_license":
        data = extract_dl_data(text)

    elif doc_type == "pancard": 
        data = extract_pan_data(text)
    
    elif doc_type == "aadhaar":
        full_aadhaar = is_full_aadhaar_card(text)
        aadhaar_back = is_aadhaar_back_side(text)

        print(f"[AADHAAR SIDE] full_card={full_aadhaar} back_side={aadhaar_back}")

        data = extract_aadhaar_data(text)

        if aadhaar_back and not full_aadhaar:
            data["name"] = None
            data["name_confidence"] = 0.0

            if (
                not data.get("id_number")
                or not data.get("address")
                or not data.get("state")
                or not data.get("pincode")
            ):
                letter_data = extract_aadhaar_letter_data(text)

                for key in ["id_number", "dob", "address", "state", "pincode", "phone", "gender"]:
                    if not data.get(key):
                        data[key] = letter_data.get(key)

                if data.get("id_number"):
                    data["is_valid_id"] = True

        else:
            if not data.get("id_number") or not data.get("name"):
                letter_data = extract_aadhaar_letter_data(text)

                for key in ["id_number", "name", "dob", "address", "state", "pincode", "phone", "gender"]:
                    if not data.get(key):
                        data[key] = letter_data.get(key)

                #if data.get("id_number"):
                 #   data["is_valid_id"] = True
    
    
    elif doc_type == "voter_id":
            data = extract_voter_strong(text)

    elif doc_type == "nepal_citizenship":
        data = extract_nepal_citizenship(text)

    elif doc_type in ["student_id", "work_id"]:
        data = extract_generic_id(text)

    else:
        return result






    # =====================================================
    # MERGE EXTRACTED DATA
    # =====================================================

    result.update(data)



    # Normalize extracted name (fix CamelCase OCR issues)
    # -----------------------------------------------------
    if result.get("name"):
        result["name"] = normalize_name_format(result["name"])
        result["name"] = sanitize_name(result["name"])


    # Aadhaar-specific safety:
    # if extracted "name" looks like address text, remove it BEFORE confidence is computed
    if doc_type == "aadhaar" and result.get("name"):
        if looks_like_aadhaar_address_fragment(result["name"]):
            print(f"[AADHAAR NAME RESET] Removed address-like name: {result['name']}")
            result["name"] = None


    # =====================================================
    # ID CONFIDENCE
    # =====================================================

    result["id_confidence"] = compute_id_confidence(
        text,
        result.get("id_number")
    )


    # =====================================================
    # NAME CONFIDENCE
    # =====================================================

    result["name_confidence"] = compute_name_confidence(
        result.get("name")
    )

    name = result.get("name")
    name_conf = result.get("name_confidence", 0)





    # Aadhaar back side: keep name empty, do not run QWEN
    #if doc_type == "aadhaar" and is_aadhaar_back_side(text):
    '''if doc_type == "aadhaar" and is_aadhaar_back_side(text) and not is_full_aadhaar_card(text):
        result["name"] = None
        result["name_confidence"] = 0.0

        if result.get("name_confidence") is not None:
            result["name_confidence"] = float(result["name_confidence"])

        if result.get("id_confidence") is not None:
            result["id_confidence"] = float(result["id_confidence"])

        if result.get("clip_score") is not None:
            result["clip_score"] = float(result["clip_score"])

        return result'''
    

    if doc_type == "aadhaar" and is_aadhaar_back_side(text) and not is_full_aadhaar_card(text):

        result["name"] = None
        result["name_confidence"] = 0.0

        if result.get("name_confidence") is not None:
            result["name_confidence"] = float(result["name_confidence"])

        if result.get("id_confidence") is not None:
            result["id_confidence"] = float(result["id_confidence"])

        if result.get("clip_score") is not None:
            result["clip_score"] = float(result["clip_score"])

        # 🔥 FINAL VALIDATION HERE
        if result.get("id_number"):
            result["is_valid_id"] = True
        elif result.get("document_type") and result["document_type"] != "unknown":
            result["is_valid_id"] = True
        else:
            result["is_valid_id"] = False

        return result


    print(f"[FALLBACK CHECK] name={name} conf={name_conf}")


    # =====================================================
    # QWEN FALLBACK (FINAL PRODUCTION VERSION 🚀)
    # =====================================================


    def needs_qwen_fallback(name, conf):
        """
        Decide whether Qwen is needed
        """
        QWEN_NAME_FALLBACK_ENABLED = True 

        if not QWEN_NAME_FALLBACK_ENABLED:
            return False

        if not name:
            return True

        # 🔥 Trigger more aggressively
        #if conf is None or conf < 0.7:
         #   return True

        if conf is None:
            return True 
        
        if conf < 0.8:
            return True   

        # Too short (likely wrong)
        #if len(name.split()) < 2:
        #    return True
        
        if not re.fullmatch(r"[A-Za-z ]+", name):
            return True

        # Garbage OCR patterns
        if any(x in name.lower() for x in [
            "wifi", "signature", "govt", "income", "tax", "department", "pan", "card",
            "government", "india", "name" 
        ]):
            return True

        return False

    print(f"[FALLBACK DECISION] {needs_qwen_fallback(name, name_conf)}")


    
    if not needs_qwen_fallback(name, name_conf):
        return result


    # ENSURE IMAGE EXISTS
    if not image_path:
        return result


    


    # =====================================================
    # RUN QWEN (PDF + IMAGE SAFE 🚀) with time calculation
    # =====================================================

    if needs_qwen_fallback(name, name_conf):

        import time
        print(f"[QWEN] Triggered for: {name} ({name_conf})")

        qwen_start_total = time.time()

        # =========================
        # PDF → IMAGE CONVERSION
        # =========================
        convert_start = time.time()

        qwen_input_path = image_path

        if images is not None and len(images) > 0:
            try:
                import tempfile
                from PIL import Image

                tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")

                Image.fromarray(images[0]).save(tmp_file.name, format="JPEG", quality=70)
                
                #Image.fromarray(images[0]["original"]).save(tmp_file.name, format="JPEG", quality=70)

                qwen_input_path = tmp_file.name

                print(f"[TIME] PDF->IMAGE: {round(time.time() - convert_start, 3)} sec")

            except Exception as e:
                print(f"[QWEN] Conversion failed: {e}")
                qwen_input_path = image_path


        # =========================
        # QWEN CALL
        # =========================
        qwen_call_start = time.time()

        try:
            qwen_name = extract_name_with_qwen(qwen_input_path)

            #qwen_name = extract_name_with_qwen_local(qwen_input_path)
        except Exception as e:
            print(f"[QWEN] Failed: {e}")
            qwen_name = None

        print(f"[TIME] QWEN CALL: {round(time.time() - qwen_call_start, 3)} sec")
        print(f"[QWEN] Raw Output: {qwen_name}")

        # =========================
        # POST PROCESS
        # =========================
        post_start = time.time()

        if qwen_name:
            qwen_name = normalize_name_format(qwen_name).strip()
            qwen_name = sanitize_name(qwen_name)

            if not qwen_name:
                print("[QWEN REJECT] sanitize_name returned None")
                qwen_name = None

            elif not re.fullmatch(r"[A-Za-z ]+", qwen_name):
                print(f"[QWEN REJECT] non alphabetic output: {qwen_name}")
                qwen_name = None

            elif len(qwen_name.split()) > 6:
                print(f"[QWEN REJECT] too many words: {qwen_name}")
                qwen_name = None

            elif len(qwen_name.split()) == 1 and len(qwen_name) < 4:
                print(f"[QWEN REJECT] single short word: {qwen_name}")
                qwen_name = None

            elif any(x in qwen_name.lower() for x in [
                "government", "india", "department", "card",
                "s/o", "d/o", "w/o", "late"
            ]):
                print(f"[QWEN REJECT] blocked word found: {qwen_name}")
                qwen_name = None

        print(f"[TIME] POST PROCESS: {round(time.time() - post_start, 3)} sec")

        # =========================
        # APPLY RESULT
        # =========================
        
        if qwen_name:
            print(f"[QWEN] Final: {qwen_name} | Conf: 0.99")
            result.update({
                "name": qwen_name,
                "name_confidence": 0.99
            })
        else:
            print("[QWEN] Rejected output")    


        # =========================
        # CLEANUP
        # =========================
        if qwen_input_path != image_path:
            import os
            try:
                os.remove(qwen_input_path)
            except:
                pass


        print(f"[TIME] TOTAL QWEN BLOCK: {round(time.time() - qwen_start_total, 3)} sec")


    # FINAL SAFETY RULE (PREVENT GARBAGE NAMES)
    

    if result.get("name_confidence", 0) < 0.3 and result.get("name"):
        result["name_confidence"] = round(
            min(result["name_confidence"], 0.2), 2
        )    


    # FIX NUMPY TYPES FOR DATABASE

    if result.get("name_confidence") is not None:
        result["name_confidence"] = float(result["name_confidence"])

    if result.get("id_confidence") is not None:
        result["id_confidence"] = float(result["id_confidence"])

    if result.get("clip_score") is not None:
        result["clip_score"] = float(result["clip_score"])    


    #print(f"[TIME] TOTAL PIPELINE: {round(time.time() - pipeline_start, 3)} sec")    


    #return result


    print(f"[TIME] TOTAL PIPELINE: {round(time.time() - pipeline_start, 3)} sec")    


    # =========================================
    # FINAL VALIDATION (SINGLE SOURCE OF TRUTH)
    # =========================================
    if result.get("id_number"):
        result["is_valid_id"] = True
    elif result.get("document_type") and result["document_type"] != "unknown":
        result["is_valid_id"] = True
    else:
        result["is_valid_id"] = False


    return result    
