from __future__ import annotations

import hashlib
import json
import os
from typing import Any


CARETAKER_LLM_UPSTREAM_URL = os.getenv(
    "LLM_UPSTREAM_URL",
    "https://app.bpai.info/api/bpai/run_llm",
)


def stable_json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def extract_llm_text(data: Any) -> str:
    if data is None:
        return ""

    if isinstance(data, str):
        return data.strip()

    if not isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False, default=str)

    for key in ("result", "response", "content", "text", "output"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for key in ("data", "message", "choices"):
        value = data.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

        if isinstance(value, dict):
            nested = extract_llm_text(value)
            if nested and nested != "{}":
                return nested

        if isinstance(value, list) and value:
            chunks = [extract_llm_text(item) for item in value]
            chunks = [chunk for chunk in chunks if chunk and chunk != "{}"]
            if chunks:
                return "\n".join(chunks).strip()

    return json.dumps(data, ensure_ascii=False, default=str)


def run_caretaker_prompt(
    prompt: str,
    *,
    model: str = "gpt-5-mini",
    timeout_seconds: int = 120,
) -> str:
    import httpx

    safe_timeout = max(30.0, float(timeout_seconds or 120))
    timeout = httpx.Timeout(
        connect=10.0,
        read=safe_timeout,
        write=30.0,
        pool=5.0,
    )

    request_payload = {
        "system_prompt": prompt,
        "payload": {
            "model": model,
            "source": "caretaker_performance_review",
        },
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(CARETAKER_LLM_UPSTREAM_URL, json=request_payload)
            response.raise_for_status()
            return extract_llm_text(response.json())

    except httpx.TimeoutException as exc:
        raise RuntimeError(f"LLM upstream timed out: {exc.__class__.__name__}") from exc

    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:300] if exc.response is not None else ""
        status = exc.response.status_code if exc.response is not None else "unknown"
        raise RuntimeError(f"LLM upstream returned {status}: {body}") from exc

    except Exception as exc:
        raise RuntimeError(f"LLM proxy error: {exc.__class__.__name__}: {exc}") from exc