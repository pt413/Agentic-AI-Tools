from __future__ import annotations

import json
import os
from typing import Any


LEAD_LLM_UPSTREAM_URL = os.getenv("LLM_UPSTREAM_URL", "https://app.bpai.info/api/bpai/run_llm")

def _stable_json_hash(value: Any) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _json_param(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _extract_llm_text(data: Any) -> str:
    """Extract text from the bpai run_llm response shape used by /llm-rating."""
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
            nested = _extract_llm_text(value)
            if nested and nested != "{}":
                return nested
        if isinstance(value, list) and value:
            chunks = [_extract_llm_text(item) for item in value]
            chunks = [chunk for chunk in chunks if chunk and chunk != "{}"]
            if chunks:
                return "\n".join(chunks).strip()

    return json.dumps(data, ensure_ascii=False, default=str)


def run_openai_prompt(prompt: str, *, model: str = "builtin", timeout_seconds: int = 120) -> str:
    """Run lead review prompt through the bpai HTTP LLM proxy.

    This avoids requiring OPENAI_API_KEY on the local FastAPI app. The upstream
    LLM service owns provider credentials.
    """
    import httpx

    safe_timeout = max(60.0, float(timeout_seconds or 120))
    timeout = httpx.Timeout(connect=10.0, read=safe_timeout, write=30.0, pool=5.0)
    request_payload = {
        "system_prompt": prompt,
        "payload": {
            "model": model,
            "source": "lead_communication_review",
        },
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(LEAD_LLM_UPSTREAM_URL, json=request_payload)
            response.raise_for_status()
            return _extract_llm_text(response.json())
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"LLM upstream timed out: {exc.__class__.__name__}") from exc
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:300] if exc.response is not None else ""
        status = exc.response.status_code if exc.response is not None else "unknown"
        raise RuntimeError(f"LLM upstream returned {status}: {body}") from exc
    except Exception as exc:
        raise RuntimeError(f"LLM proxy error: {exc.__class__.__name__}: {exc}") from exc


