import os
import json
import redis
import google.generativeai as genai
from celery.utils.log import get_task_logger
from celery import shared_task

logger = get_task_logger(__name__)

# Configure Gemini client
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Redis client
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "127.0.0.1"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    db=0
)

@shared_task
def process_new_messages():
    """
    Pull new WhatsApp messages from Redis,
    generate embeddings using Gemini,
    and print results (not storing in DB yet).
    """
    for key in redis_client.scan_iter("whatsapp:messages:*"):
        cx_number = key.decode().split(":")[-1]

        while True:
            raw_msg = redis_client.lpop(key)
            if not raw_msg:
                break

            try:
                msg = json.loads(raw_msg)
                text = msg.get("content", "").strip()

                if not text:
                    logger.info(f"[SKIP] Empty message for {cx_number}")
                    continue

                # Generate embedding with Gemini
                embedding_response = genai.embed_content(
                    model="models/embedding-001",
                    content=text
                )
                embedding = embedding_response["embedding"]

                # Just print for now
                logger.info(f"[EMBEDDING] cx_number={cx_number}, msg_id={msg['id']}")
                logger.info(f"Content: {text}")
                logger.info(f"Embedding (first 10 dims): {embedding[:10]}")
                logger.info("-" * 50)

            except Exception as e:
                logger.error(f"Error processing message for {cx_number}: {e}")
