import re

def normalize_number(n: str):
    """
    Normalize phone/jid-like strings to digits-only, 
    preserving alphanumeric LIDs and group IDs if they appear before the domain.
    """
    if not n:
        return n
    
    s = str(n).strip()
    
    # If it includes a domain, split and check the local part
    if "@" in s:
        local_part = s.split('@')[0]
        # Keep group IDs and LIDs as-is if they have alphabets
        if "@g.us" in s or "@lid" in s:
            if any(c.isalpha() for c in local_part):
                return local_part
            # Otherwise clean the digits
            s = local_part
        else:
            s = local_part

    # Handle internal separators from WhatsApp
    s = s.split(':')[0].split('-')[0]
    
    # If it contains letters, it's likely a LID or alphanumeric ID - keep it
    if any(c.isalpha() for c in s):
        return s

    # Otherwise assume it's a phone number and strip non-digits
    digits = re.sub(r"\D", "", s)
    
    # Guard: common bug where numbers are duplicated (e.g., 91948...91948...)
    while digits and len(digits) % 2 == 0:
        half = len(digits) // 2
        if digits[:half] == digits[half:]:
            digits = digits[:half]
            continue
        break
    return digits

def normalize_jid(jid_or_phone: str):
    """
    Ensure a string is a valid JID. 
    Converts pure digits or phone strings to @s.whatsapp.net. 
    Maintains @g.us or @lid if already present.
    """
    if not jid_or_phone:
        return jid_or_phone
    
    s = str(jid_or_phone).strip()
    if "@" in s:
        return s
    
    # Strip non-digits to get clean phone number
    digits = re.sub(r"\D", "", s)
    if digits:
        return f"{digits}@s.whatsapp.net"
    return s
