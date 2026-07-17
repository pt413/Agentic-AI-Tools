# app/cache/redis_client.py

import os
import redis
from app.utils.logger import get_logger

log = get_logger("redis")


REDIS_URL = os.getenv("REDIS", "redis://127.0.0.1:6379/0")

redis_client = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_timeout=0.5,            
    socket_connect_timeout=1.0,   
    health_check_interval=30,
    retry_on_timeout=True,         
    max_connections=20,
)


def is_redis_available() -> bool:
    """Check if Redis is reachable"""
    try:
        redis_client.ping()
        return True
    except Exception:
        return False


log.info(f"Redis configured: {REDIS_URL}")