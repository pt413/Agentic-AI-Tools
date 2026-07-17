from sqlalchemy.orm import Session
from app.model.chat_history import UserChatMessage
from app.generic.llm_ans import geminis
import json
def extract_final_response(response):
    """Safely extract the final response text from LLM outputs."""
    if isinstance(response, dict):
        return response.get("final_response") or response.get("response") or json.dumps(response)
    return str(response)

async def build_session_context(db: Session, chat_session_id: str, query: str, intent: str):
    """
    Build contextual prompt using the recent chat history for a given session.
    """
    # Fetch last few messages in the current session
    history = (
        db.query(UserChatMessage)
        .filter(UserChatMessage.chat_session_id == chat_session_id)
        .order_by(UserChatMessage.created_at.desc())
        .limit(5)  # Adjust depth as needed
        .all()
    )

    # Reverse to chronological order
    history = history[::-1]

    # Build chat transcript
    conversation_text = "\n".join(
        [f"User: {msg.question}\nAssistant: {msg.ans}" for msg in history if msg.ans]
    )

    # Summarize history to keep prompt short
    if conversation_text:
        summary_prompt = f"""
        Summarize the key context of the following conversation (keep factual info only, under 80 words):
        {conversation_text}
        """
        summary_response = await geminis(summary_prompt, cust_prompt="You are a summarizer.")
        summary_text = extract_final_response(summary_response)
    else:
        summary_text = ""

    # Build contextual prompt for current LLM call
    contextual_prompt = f"""
Conversation summary so far:
{summary_text}

User now says: "{query}"
Detected intent: {intent}

Respond accurately using the context above.
"""
    return contextual_prompt
