import os
import time
import random
import requests
from fastapi import HTTPException

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GEMINI_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

MAX_CHARS = int(os.getenv("SUMMARY_MAX_CHARS", "12000"))
RETRY_ATTEMPTS = int(os.getenv("SUMMARY_RETRY_ATTEMPTS", "5"))
BASE_DELAY = float(os.getenv("SUMMARY_RETRY_BASE_DELAY", "1.5"))

def summarize_text(text: str, instruction: str = None) -> str:
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="Gemini API key not set")

    if not text or not isinstance(text, str):
        raise HTTPException(status_code=400, detail="Invalid input text for summarization")

    # Trim long text
    text = text.strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]

    prompt = f"{instruction}\n\n{text}" if instruction else text

    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }

    for i in range(RETRY_ATTEMPTS):
        try:
            resp = requests.post(
                GEMINI_ENDPOINT,
                params={"key": GEMINI_API_KEY},
                json=payload,
                timeout=60,
            )
            if resp.status_code in (429, 503):
                delay = BASE_DELAY * (2 ** i) + random.uniform(0, 0.25)
                time.sleep(delay)
                continue
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                return ""
            parts = candidates[0].get("content", {}).get("parts", [])
            return "\n".join([p.get("text", "") for p in parts if isinstance(p, dict)]).strip()
        except requests.exceptions.RequestException as e:
            if i < RETRY_ATTEMPTS - 1:
                delay = BASE_DELAY * (2 ** i) + random.uniform(0, 0.25)
                time.sleep(delay)
                continue
            raise HTTPException(status_code=500, detail=f"Gemini API error: {str(e)}")
    raise HTTPException(status_code=500, detail="Failed to summarize text after retries")
