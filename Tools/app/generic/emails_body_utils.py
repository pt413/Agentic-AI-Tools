import re
import base64
import json
from bs4 import BeautifulSoup
from email_reply_parser import EmailReplyParser
from email.parser import BytesParser
from email.policy import default


# JSON FORMATTER (For emails containing JSON blocks)

def format_json_body_from_text(text: str):
    """
    Detect JSON inside email body and convert it into readable format.
    """

    json_match = re.search(r'(.*?)(\{.*\})', text.strip(), re.DOTALL)

    if not json_match:
        return None

    json_string = json_match.group(2)
    title_prefix = json_match.group(1).strip().replace(':', '')

    cleaned_string = json_string.replace('\n', '').replace('\r', '')
    cleaned_string = re.sub(r'\s*([:,])\s*', r'\1', cleaned_string)
    cleaned_string = re.sub(r'([0-9])\s+([0-9])', r'\1\2', cleaned_string)

    try:
        data = json.loads(cleaned_string)

        formatted_elements = []
        if title_prefix:
            formatted_elements.append(f"{title_prefix}:")

        for key, value in data.items():
            formatted_elements.append(
                f"{key.replace('_', ' ').title()}: {value}"
            )

        return ", ".join(formatted_elements)

    except json.JSONDecodeError:
        return None


# SAFE SIGNATURE REMOVAL (NO BLIND CUTTING)


def remove_signature_safely(text: str) -> str:

    lines = text.strip().split("\n")

    if len(lines) < 2:
        return text.strip()

    signature_keywords = ["regards", "best regards", "thanks", "cheers"]

    check_start = max(len(lines) - 5, 0)

    for i in range(len(lines) - 1, check_start - 1, -1):
        line = lines[i].strip().lower()

        for keyword in signature_keywords:
            if re.fullmatch(rf"{keyword}[.,]?", line):

                # Check next line looks like name
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()

                    if re.fullmatch(r"[A-Z][a-zA-Z]{1,20}", next_line):
                        return "\n".join(lines[:i]).strip()

                # If no name line, still remove keyword only
                return "\n".join(lines[:i]).strip()

    return text.strip()






# MAIN EMAIL BODY EXTRACTION

def extract_email_body(payload: dict) -> str:
    """
    Extracts clean, human-readable email body from Gmail API payload.
    """

    data = None

    # Try multipart first
    if payload.get("parts"):
        for part in payload["parts"]:
            if part.get("body", {}).get("data"):
                data = part["body"]["data"]
                break
    else:
        data = payload.get("body", {}).get("data")

    if not data:
        return ""

    # Decode base64
    try:
        decoded = base64.urlsafe_b64decode(data.encode("ASCII")).decode(
            "utf-8", errors="ignore"
        )
    except Exception:
        return ""

    # Remove HTML tags
    soup = BeautifulSoup(decoded, "html.parser")
    text = soup.get_text()

    # Try JSON formatting first
    formatted_json = format_json_body_from_text(text)
    if formatted_json is not None:
        return formatted_json

    # Clean non-printable characters
    text = re.sub(r'[^\x20-\x7E\n]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    # Remove quoted replies
    visible_body = EmailReplyParser.parse_reply(text)

    # Remove signature safely (FIXED LOGIC)
    visible_body = remove_signature_safely(visible_body)

    return visible_body.strip()


# RAW EMAIL PARSER (For .eml / byte input)

def email_parser(email_content_bytes: bytes) -> str:
    """
    Extract plain text from raw email bytes.
    """

    msg = BytesParser(policy=default).parsebytes(email_content_bytes)

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = part.get('Content-Disposition')

            if ctype == 'text/plain' and cdisp is None:
                return part.get_payload(decode=True).decode(errors='ignore')
    else:
        return msg.get_payload(decode=True).decode(errors='ignore')

    return "(No readable content found)"


# OPTIONAL MANUAL TEST BLOCK


'''if __name__ == "__main__":

    test_email = """

Hi Team,

Thanks for the confirmation.

"""

    print("-------- ORIGINAL --------")
    print(test_email)

    cleaned = remove_signature_safely(test_email)

    print("\n-------- CLEANED --------")
    print(cleaned)'''








'''if __name__ == "__main__":

    test_email = """

srikanth Nest
Sat, Jan 31, 8:38 PM (11 days ago)
to me

Hi Team,

Thanks for the refund amount of rs 12,320 and total deposit paid is 1500.

Renovation charges are 1180 and for current bill was around 1500 rs and it’s not acceptable and can you please share the details .how the 1500 rs current bill was calculated?



Regards,
Srikanth 
+91 9606510957
"""

    print("-------- ORIGINAL --------")
    print(test_email)

    cleaned = remove_signature_safely(test_email)

    print("\n-------- CLEANED --------")
    print(cleaned)'''
