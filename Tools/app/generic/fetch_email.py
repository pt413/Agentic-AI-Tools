import os, re, base64
from datetime import datetime
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from bs4 import BeautifulSoup
from sentence_transformers import SentenceTransformer
from transformers import pipeline
# from extract_email_body import EmailReplyParser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
CREDENTIALS_PATH = os.path.join(BASE_DIR, "../credentials/credentials.json")
TOKEN_PATH = os.path.join(BASE_DIR, "../credentials/gmailtoken.json")

embedder = SentenceTransformer('all-MiniLM-L6-v2')
summarizer = pipeline("summarization", model="facebook/bart-large-cnn")

def gmail_authenticate():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, 'w') as token:
            token.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

def summarize_text(text, max_len=120):
    try:
        if len(text.split()) < 30:
            return text
        summary = summarizer(
            text,
            max_length=max_len,
            min_length=30,
            do_sample=False
        )[0]['summary_text']
        return summary.strip()
    except Exception as e:
        print(f"⚠️ Summarization failed: {e}")
        return text

def decode_base64(data):
    return base64.urlsafe_b64decode(data.encode('ASCII')).decode('utf-8', errors='ignore')

def clean_text(raw_text):
    soup = BeautifulSoup(raw_text, "html.parser")
    text = soup.get_text()
    text = re.sub(r'[^\x20-\x7E]+', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

# def extract_email_body(payload):
#     if payload.get('parts'):
#         for part in payload['parts']:
#             data = part['body'].get('data')
#             if data:
#                 return clean_text(decode_base64(data))
#     else:
#         data = payload['body'].get('data')
#         if data:
#             return clean_text(decode_base64(data))
#     return "(No readable content found)"

from email_reply_parser import EmailReplyParser
from bs4 import BeautifulSoup
import base64
import re

# from talon import quotations
# from talon.signature.bruteforce import extract_signature

def remove_footer_lines(text):
    footer_keywords = [
        "Regards", "DISCLAIMER", "FAQs", "Policies", "Thanks", "Best Regards", "Contact RentMyStay",
        "unsubscribe", "BrightPath Technology", "M:", "E:", "Skype:"
    ]

    lines = text.splitlines()
    clean_lines = []

    footer_found = False
    for line in reversed(lines):
        if any(keyword.lower() in line.lower() for keyword in footer_keywords):
            footer_found = True
            continue
        if footer_found and line.strip() == "":
            continue  
        clean_lines.insert(0, line)  
        footer_found = False 

    return "\n".join(clean_lines)

'''def extract_email_body(payload):
    raw_text = ""
    
    if payload.get('parts'):
        for part in payload['parts']:
            data = part['body'].get('data')
            if data:
                raw_text = base64.urlsafe_b64decode(data.encode('ASCII')).decode('utf-8', errors='ignore')
                break
    else:
        data = payload['body'].get('data')
        if data:
            raw_text = base64.urlsafe_b64decode(data.encode('ASCII')).decode('utf-8', errors='ignore')

    if not raw_text:
        return "(No readable content found)"

    soup = BeautifulSoup(raw_text, "html.parser")
    text = soup.get_text()
    text = re.sub(r'[^\x20-\x7E]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    
    visible_body = EmailReplyParser.parse_reply(text)

    # print(f"After the clean {visible_body}")
    visible_body = re.split(r'(--+|Regards|Thanks|Best regards|Cheers)', visible_body, maxsplit=1)[0].strip()
    return visible_body'''
    
def extract_email_body(payload):
    if not payload:
        return ""

    html_body = None
    plain_body = None

    def walk(part):
        nonlocal html_body, plain_body

        mime = part.get("mimeType", "")
        body = part.get("body", {}).get("data")

        if body:
            decoded = base64.urlsafe_b64decode(body).decode("utf-8", errors="ignore")

            if mime == "text/html" and not html_body:
                html_body = decoded
            elif mime == "text/plain" and not plain_body:
                plain_body = decoded

        for subpart in part.get("parts", []):
            walk(subpart)

    walk(payload)

    if html_body:
        soup = BeautifulSoup(html_body, "lxml")
        for tag in soup.select("blockquote, .gmail_quote"):
            tag.decompose()

        text = soup.get_text("\n")
        text = text.replace('\xa0', ' ')
        text = re.sub(r'\s+', ' ', text).strip()

        if len(text) >= 20:
            return text

    if plain_body:
        text = re.sub(r'\s+', ' ', plain_body).strip()
        if len(text) >= 20:
            return text

    return ""  

from email.parser import BytesParser
from email.policy import default

# Example email content as bytes
# email_content = b"""From: sender@example.com
# Subject: Test Email
# Content-Type: text/plain; charset="utf-8"

# Hello, this is a test email.
# """

# # Parse the email content
# from email.parser import BytesParser
# from email.policy import default

def email_parser(email_content_bytes):
    msg = BytesParser(policy=default).parsebytes(email_content_bytes)

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = part.get('Content-Disposition')
            if ctype == 'text/plain' and cdisp is None:
                body = part.get_payload(decode=True).decode(errors='ignore')
                return body
    else:
        body = msg.get_payload(decode=True).decode(errors='ignore')
        return body

    return "(No readable content found)"


def fetch_emails(service, query=""):
    
    emails = []
    page_token = None
    user_email = service.users().getProfile(userId='me').execute()['emailAddress']

    while True:
        results = service.users().messages().list(
            userId='me',
            q=query,
            pageToken=page_token,
            maxResults=500
        ).execute()
        messages = results.get('messages', [])

        for msg in messages:
            try:
                msg_data = service.users().messages().get(
                    userId='me',
                    id=msg['id'],
                    format='full'
                ).execute()

                payload = msg_data.get('payload', {})
                headers = payload.get('headers', [])

                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '(No Subject)')
                raw_sender = next((h['value'] for h in headers if h['name'] == 'From'), '(Unknown Sender)')
                receiver_raw = next((h['value'] for h in headers if h['name'] == 'To'), '(Unknown Receiver)')

                sender = re.search(r'<([^>]+)>', raw_sender)
                sender = sender.group(1) if sender else raw_sender

                receiver = re.search(r'<([^>]+)>', receiver_raw)
                receiver = receiver.group(1) if receiver else receiver_raw

                date_str = next((h['value'] for h in headers if h['name'] == 'Date'), None)
                if date_str:
                    try:
                        date_obj = parsedate_to_datetime(date_str)  # parses almost any valid email date format
                        formatted_date = date_obj.strftime('%Y-%m-%d %H:%M:%S %Z')
                    except Exception:
                        # fallback to Gmail internalDate if header fails
                        internal_ts = int(msg_data.get('internalDate', 0)) / 1000
                        date_obj = datetime.utcfromtimestamp(internal_ts)
                        formatted_date = date_obj.strftime('%Y-%m-%d %H:%M:%S UTC')
                else:
                    # fallback to internalDate if header missing
                    internal_ts = int(msg_data.get('internalDate', 0)) / 1000
                    date_obj = datetime.utcfromtimestamp(internal_ts)
                    formatted_date = date_obj.strftime('%Y-%m-%d %H:%M:%S UTC')

                body = extract_email_body(payload)
                # summary = summarize_text(body)
                direction = "outgoing" if user_email in sender.lower() else "incoming"
                # query_vector = embedder.encode(body).tolist()
                # subject_vector=embedder.encode(subject).tolist()

                emails.append({
                    "subject": subject,
                    "direction": direction,
                    "sender": sender,
                    "receiver": receiver,
                    "date": formatted_date,
                    "body": body,
                    "snippet": msg_data.get('snippet', '')[:200],
                    "msgid": msg_data.get('id'),
                    "thread_id": msg_data.get('threadId'),
                    # "embedding": query_vector,
                    # "subject_emb": subject_vector
                 
                })
                #    "summary": summary,
                
            except Exception as e:
                print(f"Error processing message {msg['id']}: {str(e)}")
                continue

        page_token = results.get('nextPageToken')
        if not page_token:
            break

    return emails
