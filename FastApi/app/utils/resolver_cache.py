from typing import Optional
from app.db.redis_sync import redis_client

RESOLVER_CACHE_TTL = 6 * 60 * 60  # 6 hours


def _resolver_key(channel: str, identifier: str) -> str:
    return f"resolver:{channel}:{identifier}"


def get_cached_u_id(channel: str, identifier: str) -> Optional[int]:
    value = redis_client.get(_resolver_key(channel, identifier))
    return int(value) if value else None


def set_cached_u_id(channel: str, identifier: str, u_id: int):
    redis_client.setex(
        _resolver_key(channel, identifier),
        RESOLVER_CACHE_TTL,
        str(u_id),
    )
