import redis
import os

REDIS = os.getenv("REDIS")

redis_client = redis.Redis.from_url(
    REDIS,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_timeout=5,
)
