import os
import requests
from dotenv import load_dotenv
load_dotenv()

LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_API_MODEL = os.getenv("LLM_API_MODEL")

def summarize(text: str) -> str:
    """
    Sends text to the configured LLM API and returns the summary.
    Requires LLM_API_KEY and LLM_API_MODEL from environment variables.
    """
    if not LLM_API_KEY or not LLM_API_MODEL:
        raise ValueError("LLM_API_KEY or LLM_API_MODEL is not set in environment.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{LLM_API_MODEL}:generateContent?key={LLM_API_KEY}"

    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": f"Give a thorough summary of this conversation such that by analyzing it, my chatbot understands the query (entered by the user on my chatbot):\n\n{text}"}
                ]
            }
        ]
    }

    response = requests.post(url, headers=headers, json=payload)

    if response.status_code != 200:
        raise RuntimeError(
            f"Error from LLM API: {response.status_code} - {response.text}"
        )

    data = response.json()

    try:
        summary = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise RuntimeError(f"Unexpected API response format: {data}")

    return summary
