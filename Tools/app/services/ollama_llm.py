import os
import uuid
import time
import json
from typing import Any, Dict, List, Optional
import httpx
from sqlalchemy.orm import Session
from app.schemas.rag_schema import RagSearchRequest
from dotenv import load_dotenv
from app.services.rag_search import search_rag
from app.services.web_rag.web_rag_search import search_web_rag
from app.logging.tracing import ls_trace
from app.logging.logger import logger

# Load environment variables
load_dotenv()

# Define Ollama API base URL and timeout
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/api/chat")
MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
TIMEOUT = float(os.getenv("LLM_CALL_TIMEOUT_SEC", "120"))

MIN_INTERNAL_SCORE = 0.35

class OllamaProvider:
    """
    Ollama adapter that supports rewrite/planner/synthesis tasks.
    """

    def __init__(self, model: Optional[str] = None, task: str = "rewrite"):
        self.model = model or "qwen2.5:7b-instruct"
        self.task = task
        self.base_url = OLLAMA_BASE_URL
        self.timeout = TIMEOUT
        logger.info("ollama.model.selected", extra={"model": self.model, "task": self.task})

    # ============================================================
    # Public API
    # ============================================================

    async def generate(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict]] = None, **kwargs) -> Dict[str, Any]:
        with ls_trace("Ollama.generate", metadata={"model": self.model, "message_count": len(messages)}):
            system_prompt = self._get_system_prompt()

            # Prepend obedience enforcer ONLY
            final_messages = [system_prompt] + messages

            payload = {
                "model": self.model,
                "messages": final_messages,
                "stream": False,
                "options": {
                    "temperature": 0,
                    "thinking": False,
                    "num_ctx": 8192,  # Prevent prompt truncation
                },
            }

            try:
                response = await self._send_request(payload)
                raw = response.json()

                logger.debug("ollama.raw_response", extra={"raw": raw})

                msg = raw.get("message", {})
                text = msg.get("content") or msg.get("thinking")

                if not text:
                    raise ValueError("Ollama returned empty content")

                parsed = self._safe_json_parse(text)

                return self._openai_response(parsed)

            except Exception as exc:
                logger.exception("ollama.generate.failed", error=str(exc))
                return self._error_response(str(exc))

    # ============================================================
    # System prompt selection
    # ============================================================

    def _get_system_prompt(self) -> Dict[str, str]:
        if self.task == "rewrite":
            return self._rewrite_system_prompt()
        elif self.task == "planner":
            return self._planner_system_prompt()
        elif self.task == "synthesis":
            return self._synthesis_system_prompt()
        else:
            raise ValueError(f"Unsupported task: {self.task}")

    def _rewrite_system_prompt(self) -> Dict[str, str]:
        return {
            "role": "system",
            "content": (
                "You are a compiler that MUST obey ALL system instructions exactly.\n"
                "Do NOT summarize, simplify, or reinterpret rules.\n"
                "If multiple system messages exist, ALL are authoritative.\n"
                "Return ONLY valid JSON that strictly satisfies the contract.\n"
            ),
        }

    def _planner_system_prompt(self) -> Dict[str, str]:
        return {
            "role": "system",
            "content": (
                "You are a planning compiler.\n"
                "Return ONLY valid JSON.\n"
                "Do NOT explain.\n"
            ),
        }

    def _synthesis_system_prompt(self) -> Dict[str, str]:
        return {
            "role": "system",
            "content": (
                "You are a helpful assistant.\n"
                "Generate a clear, concise, user-facing answer.\n"
                "Do NOT return JSON.\n"
            ),
        }

    # ============================================================
    # JSON handling
    # ============================================================

    def _safe_json_parse(self, text: str) -> Dict[str, Any]:
        try:
            start = text.find("{")
            end = text.rfind("}") + 1
            return json.loads(text[start:end])
        except Exception:
            raise ValueError(f"Invalid JSON from Ollama: {text}")

    def _openai_response(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": f"chatcmpl-{uuid.uuid4()}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.model,
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(payload),
                    },
                    "finish_reason": "stop",
                }
            ],
        }

    def _error_response(self, message: str) -> Dict[str, Any]:
        return {
            "id": f"chatcmpl-{uuid.uuid4()}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({
                            "intent": {
                                "name": "unknown",
                                "kind": "informational",
                                "confidence": 0.0,
                                "ambiguities": ["ollama_error"],
                            },
                            "entities": {},
                            "confidence": 0.0,
                            "meta": {"error": message},
                            "schema_version": "4.1",
                        }),
                    },
                    "finish_reason": "error",
                }
            ],
        }

    # ============================================================
    # HTTP
    # ============================================================

    async def _send_request(self, payload: Dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}",
                json=payload,
            )
            response.raise_for_status()
            return response

# RAG Search function
def get_rag_results(payload, db):
    raw_results = search_rag(
        db=db,
        query=payload.query,
        top_k=payload.top_k,
        source=payload.source
    )
    formatted = []
    for r in raw_results:
        source_id = r.get("source_id", "")
        table, row_id = None, source_id
        if "_" in source_id:
            table, row_id = source_id.rsplit("_", 1)
        formatted.append({
            "table": table,
            "row_id": row_id,
            "chunk": r.get("chunk"),
            "score": r.get("score"),
            "metadata": r.get("metadata"),
        })
    return formatted

# LLM Answer Generation using Ollama
async def gen_llm_answer(payload: RagSearchRequest, db: Session):
    query = payload.query.strip()
    top_k = payload.top_k
    rag_mode = getattr(payload, "rag_mode", None)
    rag_results = []
    
    # Retrieve RAG results based on the mode (internal, external, both)
    if rag_mode == "internal":
        rag_results = search_rag(db=db, query=query, top_k=top_k, source=payload.source)
    elif rag_mode == "external":
        rag_results = search_web_rag(query=query, top_k=top_k)
    elif rag_mode == "both":
        internal_results = search_rag(db=db, query=query, top_k=top_k, source=payload.source)
        external_results = search_web_rag(query=query, top_k=top_k)
        rag_results = internal_results + external_results
    else:
        internal_results = search_rag(db=db, query=query, top_k=top_k, source=payload.source)
        if internal_results:
            rag_results = internal_results
        else:
            rag_results = search_web_rag(query=query, top_k=top_k)
    
    if not rag_results:
        return "No relevant data found."
    
    context_blocks = []
    for r in rag_results:
        source = r.get("source", "unknown")
        source_id = r.get("source_id", "na")
        context_blocks.append(f"[{source}:{source_id}]\n{r['chunk']}")
    
    context = "\n\n".join(context_blocks)
    
    prompt = f"""
        You are an AI assistant answering questions using retrieved knowledge.

        QUESTION:
        {query}

        CONTEXT:
        {context}

        Rules:
        - Use only the context
        - If the answer is not in the context, say you don't know
        - Be concise and professional
    """
    
    # Call Ollama's generate method
    ollama_provider = OllamaProvider(model="qwen2.5:7b-instruct", task="synthesis")
    response = await ollama_provider.generate(messages=[{"role": "user", "content": prompt}])
    
    # Extract the assistant's response from the Ollama API
    return response.get("choices", [{}])[0].get("message", {}).get("content", "No response from Ollama")
