import google.generativeai as genai
import os
import requests
import logging
import re
from sqlalchemy.orm import Session
from app.schemas.rag_schema import RagSearchRequest
from dotenv import load_dotenv
from app.services.rag_search import search_rag
from app.services.web_rag.web_rag_search import search_web_rag

load_dotenv()

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-2.5-flash")
MIN_INTERNAL_SCORE = 0.35

def gen_llm_answer(
    payload: RagSearchRequest,
    db: Session
):
    query = payload.query.strip()
    top_k = payload.top_k
    rag_mode = getattr(payload, "rag_mode", None)
    rag_results = []
    if rag_mode == "internal":
        rag_results = search_rag(
            db=db,
            query=query,
            top_k=top_k,
            source=payload.source
        )
    elif rag_mode == "external":
        rag_results = search_web_rag(
            query=query,
            top_k=top_k
        )
    elif rag_mode == "both":
        internal_results = search_rag(
            db=db,
            query=query,
            top_k=top_k,
            source=payload.source
        )
        external_results = search_web_rag(
            query=query,
            top_k=top_k
        )
        rag_results = internal_results + external_results
    else:
        internal_results = search_rag(
            db=db,
            query=query,
            top_k=top_k,
            source=payload.source
        )
        if (internal_results):
            rag_results = internal_results
        else:
            rag_results=search_web_rag(
                query=query,
                top_k=top_k
            )
    if not rag_results:
        return {
            "answer": "No relevant data found.",
            "source": ""
        }
    flat_results = []
    if isinstance(rag_results, dict):
        for value in rag_results.values():
            if isinstance(value, list):
                flat_results.extend(value)
    else:
        flat_results = rag_results

    if not flat_results:
        return {
            "answer": "No relevant data found.",
            "source": ""
        }
    context_blocks = []
    for r in flat_results:
        source = r.get("source","unknown")
        source_id = r.get("source_id","na")
        context_blocks.append(
            f"[{source}:{source_id}]\n{r['chunk']}\n\n"
        )
    context = "\n\n".join(context_blocks)
    prompt = f"""You are an AI assistant answering questions using retrieved knowledge.

        QUESTION:
        {query}

        CONTEXT:
        {context}

        Rules:
        - Use only the context
        - If the answer is not in the context, say you don't know
        - Be concise and professional
        """
    # try:
    #     res = model.generate_content(prompt)
    #     return res.text
    # except Exception as e:
    #     logging.warning(f"Gemini failed, falling back to Ollama: {e}")
    return {
        "answer": call_ollama(prompt),
        "source": context
    }

def call_ollama(prompt: str) -> str:
    OLLAMA_URL = os.getenv("OLLAMA_BASE_URL")
    payload = {
        "model": os.getenv("OLLAMA_MODEL"),
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "stream": False
    }
    res = requests.post(OLLAMA_URL, json=payload, timeout=500)
    res.raise_for_status()
    raw = res.json()
    return raw.get("message", {}).get("content", "").strip()