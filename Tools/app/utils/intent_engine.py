import numpy as np
from app.generic.embedding import intent_generate_embedding, cosine_similarity
from app.utils.intent_repository import get_all_intents, store_intent
from app.generic.llm_ans import geminis

SIMILARITY_THRESHOLD = 0.75

async def detect_query_intent(query: str, user_context: str, db):
    """Detect user intent using embeddings, fallback to Gemini if needed."""
    # Step 1: Fetch existing intents
    intents = get_all_intents(db)
    query_emb = await intent_generate_embedding(query)
    best_match, best_score = None, 0

    for intent in intents:
        emb = np.array(intent.embedding)
        sim = await cosine_similarity(query_emb, emb)
        if sim > best_score:
            best_match, best_score = intent, sim

    # Step 2: If found similar intent
    if best_match and best_score >= SIMILARITY_THRESHOLD:
        print(f"🎯 Matched intent: {best_match.name} (sim={best_score:.2f})")
        return best_match.name

    # Step 3: Fallback → use Gemini for new intent extraction
    system_prompt = (
        "You have to extract the intent of a user's query."
    )

    # Using your Gemini function
    intent_name = await geminis(
        prompt=f"User Context:\n{user_context}\n\nQuery:\n{query}",
        cust_prompt=system_prompt
    )

    # Step 4: Save to DB
    if intent_name and query_emb:
        store_intent(db, name=intent_name, example_query=query, embedding=query_emb)

    return intent_name.strip()
