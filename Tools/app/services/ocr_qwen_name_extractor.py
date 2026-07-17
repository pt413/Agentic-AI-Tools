'''import base64
import requests
import os
from dotenv import load_dotenv
import time

load_dotenv()   # ✅ THIS IS THE FIX

QWEN_ENABLED = os.getenv("QWEN_ENABLED", "false").lower() == "true"

QWEN_URL = os.getenv(
    "QWEN_URL",
    "http://localhost:11434/v1/chat/completions"
)


QWEN_TIMEOUT = 60  # 🔥 keep low to avoid slow API 600


# =====================================================
# UTIL
# =====================================================

def encode_image(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


# =====================================================
# MAIN FUNCTION
# =====================================================


def extract_name_with_qwen(image_path: str) -> str:
    
    # 🔴 HARD DISABLE (LOCAL DEV SAFE)
    if not QWEN_ENABLED:
        return None

    try:
        # =========================
        # ENCODE TIME
        # =========================
        encode_start = time.time()
        image_b64 = encode_image(image_path)
        print(f"[TIME] ENCODE: {round(time.time() - encode_start, 3)} sec")

        payload = {
            "model": "qwen2.5vl",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": """
                            Extract ONLY the person's FULL NAME from this Indian ID card.

                            Rules:
                            - Return only the name
                            - No extra words
                            - No labels like 'Name:'
                            - No explanation
                            - Name must be 2–4 words
                            - Ignore government text like 'INCOME TAX DEPARTMENT'

                            If no clear name, return: NULL
                            """
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_b64}"
                            }
                        }
                    ]
                }
            ]
        }

        # =========================
        # API CALL TIME
        # =========================
        api_start = time.time()

        response = requests.post(
            QWEN_URL,
            json=payload,
            timeout=QWEN_TIMEOUT
        )

        print(f"[TIME] API CALL: {round(time.time() - api_start, 3)} sec")

        # 🔴 Fail fast if bad response
        if response.status_code != 200:
            print(f"QWEN ERROR: HTTP {response.status_code}")
            return None

        result = response.json()

        # 🔴 Safe parsing
        text = (
            result.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )

        if not text:
            return None

        # 🔴 Basic cleanup
        text = text.replace("\n", " ").strip()

        return text

    except Exception as e:
        print("QWEN ERROR:", e)
        return None'''














# =====================================================
# QWEN LOCAL GPU EXTRACTOR (FINAL PRODUCTION VERSION)
# =====================================================

from app.services.qwen_vl_service import QwenVLService

import re
# 🔥 Singleton instance (loads model ONLY once)
qwen_service = QwenVLService.get_instance()


# =====================================================
# MAIN FUNCTION (USED IN PIPELINE)
# =====================================================

#def extract_name_with_qwen(image_path: str) -> str:
def extract_name_with_qwen(image_path: str, prompt: str = None) -> str:
    """
    Extract name using local Qwen2.5-VL model (GPU)

    Args:
        image_path (str): path to image

    Returns:
        str | None: extracted name or None
    """

    try:
        #result = qwen_service.extract_name(image_path)
        result = qwen_service.extract_name(image_path, prompt=prompt)

        if not result:
            return None

        # 🔥 Basic cleanup (minimal — your main pipeline already handles cleaning)
        result = result.strip()

        if result.lower() in ["null", "none", ""]:
            return None
        
        if not re.search(r"[A-Za-z]", result):
            return None

        return result

    except Exception as e:
        print("QWEN LOCAL ERROR:", e)
        return None