from app.utils.text_chunker import chunk_text
from sqlalchemy.orm import Session
from app.model.emails import Email
import os
import aiohttp
from dotenv import load_dotenv
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# Load environment variables
load_dotenv()

# Gemini model and endpoint
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GEMINI_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# Service account credentials
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_ACCOUNT_FILE = os.path.join(BASE_DIR, "../../credentials/service-account.json")

credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE,
    scopes=["https://www.googleapis.com/auth/ai.generativelanguage"]
)

async def send_to_gemini(text: str) -> str:
    """
    Sends a text chunk to Gemini API for summarization and returns the summary.
    """
    if not text.strip():
        return ""

    # Refresh token for each request
    credentials.refresh(Request())
    access_token = credentials.token

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    payload = {
        "prompt": f"Summarize the following email text in 50 words:\n{text}",
        "temperature": 0.2,
        "candidate_count": 1,
        "max_output_tokens": 200
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(GEMINI_ENDPOINT, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("candidates", [{}])[0].get("content", "")
                else:
                    print(f"⚠️ Gemini API error {resp.status}: {await resp.text()}")
                    return text  # fallback
        except Exception as e:
            print(f"⚠️ Gemini API call failed: {e}")
            return text  # fallback

async def fetch_emails_for_customer(cx_email: str, db: Session) -> str:
    """
    Fetch all emails for a customer and combine into a single string.
    """
    emails = db.query(Email).filter(Email.receiver == cx_email).order_by(Email.date).all()
    if not emails:
        return ""

    combined = "\n\n".join(str(e.body) for e in emails if e.body)
    return combined

async def summarize_customer_emails(all_emails_text: str) -> str:
    """
    Chunk the combined emails and send each chunk to Gemini for summarization.
    """
    chunks = chunk_text(all_emails_text, max_chars=1000)
    summaries = []
    for chunk in chunks:
        summary = await send_to_gemini(chunk)
        summaries.append(summary)
    return " ".join(summaries)
