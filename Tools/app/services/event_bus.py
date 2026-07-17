import os
import json
import asyncio
import logging
from typing import Optional

import redis.asyncio as redis

from app.services.sse_manager import sse_manager

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
CHANNEL = os.getenv("WHATSAPP_EVENTS_CHANNEL", "whatsapp_events")

_redis_clients: dict[int, redis.Redis] = {}
_listener_task: Optional[asyncio.Task] = None


async def get_redis() -> redis.Redis:
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    client = _redis_clients.get(loop_id)
    if client is None:
        client = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            health_check_interval=30,
        )
        _redis_clients[loop_id] = client
    return client


async def publish_event(event_type: str, data: dict):
    """
    Publish event to Redis so all FastAPI workers can receive it.
    """
    try:
        client = await get_redis()
        payload = {
            "event": event_type,
            "data": data or {},
        }
        await client.publish(CHANNEL, json.dumps(payload, default=str))
    except Exception:
        logger.exception("Failed to publish event to Redis")


def publish_event_from_sync(event_type: str, data: dict):
    try:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(publish_event(event_type, data))
        except RuntimeError:
            asyncio.run(publish_event(event_type, data))
    except Exception:
        logger.exception("Failed to publish event")


async def redis_listener_loop():
    """
    Every FastAPI worker runs this listener.
    When Redis event arrives, forward it to that worker's local SSE clients.
    """
    while True:
        try:
            client = await get_redis()
            pubsub = client.pubsub()
            await pubsub.subscribe(CHANNEL)

            logger.info("Subscribed to Redis channel: %s", CHANNEL)

            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue

                try:
                    payload = json.loads(message.get("data") or "{}")
                    event_type = payload.get("event")
                    data = payload.get("data") or {}

                    if event_type:
                        sse_manager.broadcast_local(event_type, data)

                except Exception:
                    logger.exception("Failed to process Redis pubsub message")

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Redis listener crashed; retrying in 5 seconds")
            await asyncio.sleep(5)


async def close_redis_clients() -> None:
    global _redis_clients
    for client in list(_redis_clients.values()):
        try:
            await client.close()
        except Exception:
            logger.exception("Failed to close Redis client")
    _redis_clients.clear()


def start_event_bus_listener():
    global _listener_task

    if _listener_task and not _listener_task.done():
        return _listener_task

    _listener_task = asyncio.create_task(redis_listener_loop())
    return _listener_task
