
import re
from typing import Tuple, Optional
from app.utils.logger import get_logger

log = get_logger(__name__)


_digits_re = re.compile(r'\D+')
_email_re = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def digits_only(s: str) -> str:
    """
    Return only the digits from the input, or an empty string for falsy input.
    """
    if not s:
        return ""
    return _digits_re.sub("", s)

def normalize_phone_to_10(phone: str) -> Tuple[Optional[str], str]:
    """
    Conservative normalization to a 10-digit mobile number.
    Returns: (normalized_10_digit_or_None, raw_trimmed_input)

    Rules:
      - Strip non-digits.
      - If length == 10 -> accept and return digits.
      - If length == 11 and starts with '0' -> drop leading '0' and return last 10 digits.
      - If length == 12 and starts with '91' -> return last 10 digits.
      - If length == 13 and starts with '091' -> return last 10 digits (rare).
      - Otherwise: return None (can't normalize reliably).
    """
    raw = (phone or "").strip()
    if not raw:
        return None, raw
    digits = digits_only(raw)
    if not digits:
        return None, raw

    
    if len(digits) == 10:
        return digits, raw

    
    if len(digits) == 11 and digits.startswith("0"):
        return digits[-10:], raw
    if len(digits) == 12 and digits.startswith("91"):
        return digits[-10:], raw
    if len(digits) == 13 and digits.startswith("091"):
        return digits[-10:], raw

    
    if raw.startswith("+"):
        stripped = digits  
        if len(stripped) > 10 and stripped.endswith(stripped[-10:]):
            
            if len(stripped) >= 10:
                return stripped[-10:], raw

    
    log.debug("phone_normalize_failed", extra={"raw": raw, "digits": digits})
    return None, raw

# def normalize_whatsapp_number(wa: str) -> Tuple[Optional[str], str]:
#     """
#     Wrapper for whatsapp numbers. Keeps same semantics as phone normalization
#     but also strips common ':+-' characters and whitespace.
#     """
#     return normalize_phone_to_10(wa)

def normalize_email(email: str) -> Optional[str]:
    """
    Lowercase-trim the email and do a light validation.
    Returns normalized email or None if invalid/empty.
    """
    if not email:
        return None
    e = email.strip().lower()
    if is_valid_email(e):
        return e
    log.debug("email_normalize_failed", extra={"raw": email})
    return None

def is_valid_email(email: str) -> bool:
    """
    Very light-weight email validation (no external libs).
    """
    if not email:
        return False
    return bool(_email_re.match(email))

def sanitize_content(content: Optional[str]) -> Optional[str]:
    """
    Minimal sanitization for text content:
      - Trim surrounding whitespace
      - Convert empty or whitespace-only strings to None
    Keep this conservative because we don't want to alter transcripts/messages.
    """
    if content is None:
        return None
    s = content.strip()
    return s if s else None