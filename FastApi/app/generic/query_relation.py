from app.generic.llm_ans import geminis
from app.generic.embedding import intent_generate_embedding, cosine_similarity
import json
import re

async def check_query_relation(previous_query: str, new_query: str):
    """Determine whether the new query is a follow-up or a new topic."""

    if not previous_query or not previous_query.strip():
        return {"relation": "new", "reason": "No previous query available"}

    # 1️⃣ Embedding similarity + keyword heuristic
    prev_emb = await intent_generate_embedding(previous_query)
    new_emb = await intent_generate_embedding(new_query)
    sim = await cosine_similarity(prev_emb, new_emb)

    followup_keywords = ["above", "same", "that", "those", "mentioned", "earlier", "previous"]
    if sim > 0.70 or any(word in new_query.lower() for word in followup_keywords):
        return {"relation": "follow-up", "reason": f"Semantic link or keyword cue (sim={sim:.2f})"}

    # 2️⃣ LLM fallback
    system_prompt = """
    You are a classifier. Determine if the new query is related to or depends on the previous query.
    If it continues or references the previous one, mark as "follow-up". Otherwise mark as "new".
    Respond ONLY in this strict JSON format:
    {"relation": "follow-up" or "new", "reason": "<short reason>"}
    """

    combined_prompt = f"""
    Previous query: "{previous_query.strip()}"
    New query: "{new_query.strip()}"
    """

    try:
        llm_response = await geminis(combined_prompt, system_prompt)

        # Handle possible structured or verbose outputs
        if isinstance(llm_response, dict) and "final_response" in llm_response:
            llm_response = llm_response["final_response"]

        # Extract JSON substring in case of extra text
        json_match = re.search(r"\{.*\}", str(llm_response), re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group(0).replace("'", '"'))
            if "relation" in parsed:
                return parsed

    except Exception as e:
        return {"relation": "new", "reason": f"Gemini parsing error: {e}"}

    # Default fallback
    return {"relation": "new", "reason": "Unable to determine relation from Gemini"}
