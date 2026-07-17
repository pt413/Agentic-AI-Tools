
import re
from datetime import datetime


MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

MONTH_RE = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)


def clean_ocr_text(text: str) -> str:
    if not text:
        return ""

    text = text.replace("\n", " ")
    text = text.replace("\r", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _to_number(value_text: str):
    if not value_text:
        return None

    raw = value_text.strip().replace(",", "")

    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", raw):
        return None

    try:
        value = float(raw)
    except ValueError:
        return None

    if value <= 0:
        return None

    # Avoid huge UTR / transaction IDs becoming amount.
    if value > 10000000:
        return None

    if value.is_integer():
        return int(value)

    return value


def _extract_numeric_value(line: str):
    if not line:
        return None

    patterns = [
        r"\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?",
        r"\d+\.\d{1,2}",
        r"\d+",
    ]

    for pattern in patterns:
        match = re.search(pattern, line)
        if not match:
            continue

        raw_value = match.group(0)
        digits_only = re.sub(r"\D", "", raw_value)

        # Most transaction IDs / UTR / RRN are long.
        if len(digits_only) > 8:
            continue

        return _to_number(raw_value)

    return None


def _looks_like_date_or_time(line: str) -> bool:
    if not line:
        return False

    compact = re.sub(r"\s+", "", line.lower())

    # 09:01PM, 04:22PM, 6:51pm
    if re.search(r"\d{1,2}:\d{2}", compact):
        return True

    # 31Mar2026, 31March2026, Mar31, March31
    if re.search(MONTH_RE, compact, re.I):
        return True

    # 31/03/2026 or 31-03-26
    if re.search(r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?", line):
        return True

    # A standalone year should not become amount.
    if re.fullmatch(r"\s*(?:19|20)\d{2}\s*", line):
        return True

    return False


def _is_bad_amount_context(line: str) -> bool:
    lower = (line or "").lower()

    bad_keywords = [
        "ref",
        "utr",
        "rrn",
        "transaction",
        "txn",
        "googletransaction",
        " id",
        "id:",
        "acc",
        "account",
        "bank",
        "seconds",
        "second",
        "upiid",
        "upi id",
        "upi transaction",
        "no.",
        "no:",
        "mobile",
        "phone",
        "completedon",
        "completed on",
    ]

    return any(keyword in lower for keyword in bad_keywords)


def _is_valid_amount_line(line: str) -> bool:
    if not line or not line.strip():
        return False

    line = line.strip()

    if _is_bad_amount_context(line):
        return False

    if _looks_like_date_or_time(line):
        return False

    value = _extract_numeric_value(line)
    if value is None:
        return False

    # Allow these words only because some apps show "2,000.00 sent".
    check_line = re.sub(
        r"\b(?:rs|inr|sent|paid|amount)\b",
        "",
        line,
        flags=re.I,
    )

    # Reject meaningful text lines like "StateBankofIndia6249".
    if re.search(r"[A-Za-z]", check_line):
        return False

    # Allow OCR garbage symbols like ?963 / 天500,
    # but reject ASCII letters around the number.
    leftover = re.sub(r"[\d\s,.\u20b9?*#\-+/\\|:;()\[\]{}]", "", line)
    leftover_ascii = re.sub(r"[^\x00-\x7F]", "", leftover)

    if leftover_ascii:
        return False

    return True


def extract_amount(text: str):
    raw_text = text or ""
    clean_text = clean_ocr_text(raw_text)

    # Strong explicit amount patterns.
    explicit_patterns = [
        r"(?:₹|rs\.?|inr)\s*([^\s]{0,3}\s*\d[\d,]*(?:\.\d{1,2})?)",
        r"(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s*(?:sent|paid)",
    ]

    for pattern in explicit_patterns:
        for match in re.finditer(pattern, clean_text, re.I):
            candidate_text = match.group(1)

            if _looks_like_date_or_time(candidate_text):
                continue

            value = _extract_numeric_value(candidate_text)
            if value is not None:
                return value

    # Standalone amount line detection.
    # Handles:
    # ?963
    # 天500
    # 4,990
    # 1,050
    # 21,000
    # 68,000
    # 1,000.00
    lines = [line.strip() for line in raw_text.splitlines()]

    candidates = []

    for index, line in enumerate(lines):
        if not _is_valid_amount_line(line):
            continue

        value = _extract_numeric_value(line)
        if value is None:
            continue

        score = 10

        if "," in line:
            score += 4

        if "." in line:
            score += 2

        previous_context = " ".join(lines[max(0, index - 3):index]).lower()
        next_context = " ".join(lines[index + 1:index + 4]).lower()

        if any(keyword in previous_context for keyword in [
            "paid to",
            "to:",
            "to ",
            "payment successful",
            "transaction successful",
            "transactionsuccessful",
            "completed",
        ]):
            score += 5

        if any(keyword in next_context for keyword in [
            "paid to",
            "to:",
            "transfer details",
            "payment details",
            "completed",
            "transaction",
        ]):
            score += 3

        candidates.append((score, index, value))

    if candidates:
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][2]

    return None


'''def extract_transaction_id(text: str):
    clean_text = clean_ocr_text(text)

    patterns = [
        r"upi\s*transaction\s*id[:\s]*([a-z0-9]+)",
        r"transaction\s*id[:\s]*([a-z0-9]+)",
        r"txn\s*id[:\s]*([a-z0-9]+)",
        r"google\s*transaction\s*id[:\s]*([a-z0-9]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, clean_text, re.I)
        if match:
            return match.group(1)

    return None'''


def extract_transaction_id(text: str):
    clean_text = clean_ocr_text(text)

    patterns = [
        # HDFC Transaction ID / HDFCTransactionID
        r"hdfc\s*transaction\s*id\s*[:\-]?\s*([a-z0-9]{6,40})",

        # UPI / normal transaction IDs
        r"upi\s*transaction\s*id\s*[:\-]?\s*([a-z0-9]{6,40})",
        r"google\s*transaction\s*id\s*[:\-]?\s*([a-z0-9]{6,40})",
        r"transaction\s*id\s*[:\-]?\s*([a-z0-9]{6,40})",
        r"txn\s*id\s*[:\-]?\s*([a-z0-9]{6,40})",
    ]

    for pattern in patterns:
        match = re.search(pattern, clean_text, re.I)
        if match:
            return match.group(1)

    return None



def extract_utr(text: str):
    clean_text = clean_ocr_text(text)

    match = re.search(r"utr[:\s]*([a-z0-9]+)", clean_text, re.I)
    return match.group(1) if match else None


'''def extract_rrn(text: str):
    clean_text = clean_ocr_text(text)

    patterns = [
        r"upi\s*ref\s*no[:\s]*([a-z0-9]+)",
        r"ref\.?\s*no[:\s]*([a-z0-9]+)",
        r"reference\s*no[:\s]*([a-z0-9]+)",
        r"rrn[:\s]*([a-z0-9]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, clean_text, re.I)
        if match:
            return match.group(1)

    return None'''


def extract_rrn(text: str):
    clean_text = clean_ocr_text(text)

    patterns = [
        # Your HDFC IMPS case:
        # Reference Number 615729996584
        r"reference\s*number\s*[:\-]?\s*([a-z0-9]{8,30})",

        # Other common cases
        r"reference\s*no\.?\s*[:\-]?\s*([a-z0-9]{8,30})",
        r"upi\s*ref\s*no\.?\s*[:\-]?\s*([a-z0-9]{8,30})",
        r"ref\.?\s*no\.?\s*[:\-]?\s*([a-z0-9]{8,30})",
        r"rrn\s*[:\-]?\s*([a-z0-9]{8,30})",
    ]

    for pattern in patterns:
        match = re.search(pattern, clean_text, re.I)
        if match:
            return match.group(1)

    return None    


def extract_status(text: str):
    clean_text = clean_ocr_text(text).lower()

    if any(keyword in clean_text for keyword in [
        "payment successful",
        "paymentsuccessful",
        "paid successfully",
        "transaction successful",
        "transactionsuccessful",
        "success",
    ]):
        return "success"

    if "completed" in clean_text:
        return "completed"

    if any(keyword in clean_text for keyword in [
        "failed",
        "transaction failed",
        "payment failed",
    ]):
        return "failed"

    if any(keyword in clean_text for keyword in [
        "pending",
        "processing",
        "in progress",
    ]):
        return "pending"

    return None


def _normalize_datetime_text(text: str) -> str:
    value = text or ""

    value = value.replace("\n", " ")
    value = value.replace("\r", " ")

    # 09:01PMon31Mar2026 -> 09:01 PM on 31Mar2026
    value = re.sub(
        r"(?i)(\d{1,2}:\d{2})\s*([ap])\.?\s*m\s*on\s*",
        r"\1 \2M on ",
        value,
    )

    # Completedon31March2026 -> Completed on 31March2026
    value = re.sub(r"(?i)(completed)\s*on", r"\1 on ", value)

    # TransactionSuccessful09:01... -> TransactionSuccessful 09:01...
    value = re.sub(
        r"(?i)(transactionsuccessful|paymentsuccessful)",
        r"\1 ",
        value,
    )

    value = re.sub(r"(?i)\bat\b", " ", value)

    # 31Mar2026 -> 31 Mar2026
    # 01Apr,04:22PM -> 01 Apr,04:22PM
    value = re.sub(
        rf"(?i)(\d{{1,2}})\s*(?=({MONTH_RE}))",
        r"\1 ",
        value,
    )

    # Mar2026 -> Mar 2026
    value = re.sub(
        rf"(?i)\b({MONTH_RE})\s*(?=\d{{4}}\b)",
        r"\1 ",
        value,
    )

    # Mar31 -> Mar 31
    value = re.sub(
        rf"(?i)\b({MONTH_RE})\s*(?=\d{{1,2}}\b)",
        r"\1 ",
        value,
    )

    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _month_to_int(month_text: str):
    if not month_text:
        return None

    key = re.sub(r"[^a-z]", "", month_text.lower())
    return MONTHS.get(key)


def _year_to_int(year_text=None, default_year=None):
    if not year_text:
        return default_year or datetime.now().year

    year = int(year_text)

    if year < 100:
        if year < 70:
            return 2000 + year
        return 1900 + year

    return year


def _ampm_to_24_hour(hour_text: str, ampm_text=None):
    hour = int(hour_text)

    if not ampm_text:
        return hour

    marker = re.sub(r"[^apm]", "", ampm_text.lower())

    if marker.startswith("p") and hour != 12:
        return hour + 12

    if marker.startswith("a") and hour == 12:
        return 0

    return hour


def _infer_default_year(text: str, default_year=None):
    if default_year:
        return default_year

    years = re.findall(r"\b(20\d{2}|19\d{2})\b", text or "")
    if years:
        return int(years[0])

    return datetime.now().year


def _build_datetime_from_match(match, default_year):
    data = match.groupdict()

    try:
        if data.get("month_num"):
            month = int(data["month_num"])
        else:
            month = _month_to_int(data.get("month"))

        if not month:
            return None

        day = int(data["day"])
        year = _year_to_int(data.get("year"), default_year)
        hour = _ampm_to_24_hour(data["hour"], data.get("ampm"))
        minute = int(data["minute"])

        return datetime(year, month, day, hour, minute).isoformat()

    except Exception:
        return None


def extract_transaction_datetime(text: str, default_year=None):
    clean_text = _normalize_datetime_text(text)
    default_year = _infer_default_year(clean_text, default_year)

    ampm_pattern = r"(?P<ampm>[AaPp]\.?\s*[Mm]\.?)"

    patterns = [
        # 09:01 PM on 31 Mar 2026
        rf"\b(?P<hour>\d{{1,2}}):(?P<minute>\d{{2}})\s*"
        rf"{ampm_pattern}\s*(?:on)?\s*"
        rf"(?P<day>\d{{1,2}})\s*"
        rf"(?P<month>{MONTH_RE})\s*,?\s*"
        rf"(?P<year>\d{{4}})\b",

        # 31 Mar 2026, 3:06pm
        # 1 Apr 2026, 12:56pm
        rf"\b(?P<day>\d{{1,2}})\s*"
        rf"(?P<month>{MONTH_RE})\s*,?\s*"
        rf"(?P<year>\d{{4}})\s*,?\s*"
        rf"(?P<hour>\d{{1,2}}):(?P<minute>\d{{2}})\s*"
        rf"{ampm_pattern}\b",

        # 01 Apr, 04:22PM
        # 27 Mar, 06:29 PM
        rf"\b(?P<day>\d{{1,2}})\s*"
        rf"(?P<month>{MONTH_RE})\s*,?\s*"
        rf"(?P<hour>\d{{1,2}}):(?P<minute>\d{{2}})\s*"
        rf"{ampm_pattern}\b",

        # Mar 31, 2026, 4:09 PM
        rf"\b(?P<month>{MONTH_RE})\s*"
        rf"(?P<day>\d{{1,2}})\s*,?\s*"
        rf"(?P<year>\d{{4}})\s*,?\s*"
        rf"(?P<hour>\d{{1,2}}):(?P<minute>\d{{2}})\s*"
        rf"{ampm_pattern}\b",

        # Mar 31, 4:09 PM
        rf"\b(?P<month>{MONTH_RE})\s*"
        rf"(?P<day>\d{{1,2}})\s*,?\s*"
        rf"(?P<hour>\d{{1,2}}):(?P<minute>\d{{2}})\s*"
        rf"{ampm_pattern}\b",

        # 31/03/2026 4:09 PM
        # 31-03-2026 16:09
        r"\b(?P<day>\d{1,2})[/-]"
        r"(?P<month_num>\d{1,2})[/-]"
        r"(?P<year>\d{2,4})\s*,?\s*"
        r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*"
        r"(?P<ampm>[AaPp]\.?\s*[Mm]\.?)?\b",

        # 2026-03-31 16:09
        # 2026/03/31 4:09 PM
        r"\b(?P<year>\d{4})[/-]"
        r"(?P<month_num>\d{1,2})[/-]"
        r"(?P<day>\d{1,2})\s*,?\s*"
        r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*"
        r"(?P<ampm>[AaPp]\.?\s*[Mm]\.?)?\b",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, clean_text, re.I):
            parsed = _build_datetime_from_match(match, default_year)
            if parsed:
                return parsed

    return None


def extract_payment_metadata(text: str, default_year=None):
    raw_text = text or ""

    return {
        "amount": extract_amount(raw_text),
        "transaction_id": extract_transaction_id(raw_text),
        "utr": extract_utr(raw_text),
        "rrn": extract_rrn(raw_text),
        "transaction_datetime": extract_transaction_datetime(
            raw_text,
            default_year=default_year,
        ),
        "status": extract_status(raw_text),
    }
