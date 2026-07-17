import time
import uuid
import json
import hashlib
from datetime import datetime
from typing import List, Optional

from app.db.clickhouse_client import get_ch_client
from app.utils.logger import get_logger
from app.utils.metrics import (
    conversation_requests_total,
    conversation_failures_total,
    conversation_duration_ms,
    conversation_db_query_duration_ms,
    conversation_rows_returned,
)
from app.db.redis import redis_client

log = get_logger("conversation_service")

CACHE_TTL = 300


BASE_QUERY = """
SELECT
    activity_id,
    channel,
    sender,
    receiver,
    content,
    timestamp
FROM uni_activity
WHERE _peerdb_is_deleted = 0
  AND channel IN %(channels)s
  AND (%(from_date)s IS NULL OR timestamp >= %(from_date)s)
  AND (%(to_date)s IS NULL OR timestamp <= %(to_date)s)
  AND (
        (
            %(direction)s = 'sent' AND (
                (sender = %(id1)s AND receiver = %(id2)s)
                OR (
                    sen_id IN (
                        SELECT u_id FROM user_data
                        WHERE email = %(id1)s OR phone = %(id1)s OR wa_num = %(id1)s
                    )
                    AND rec_id IN (
                        SELECT u_id FROM user_data
                        WHERE email = %(id2)s OR phone = %(id2)s OR wa_num = %(id2)s
                    )
                )
            )
        )
        OR
        (
            %(direction)s = 'received' AND (
                (sender = %(id2)s AND receiver = %(id1)s)
                OR (
                    sen_id IN (
                        SELECT u_id FROM user_data
                        WHERE email = %(id2)s OR phone = %(id2)s OR wa_num = %(id2)s
                    )
                    AND rec_id IN (
                        SELECT u_id FROM user_data
                        WHERE email = %(id1)s OR phone = %(id1)s OR wa_num = %(id1)s
                    )
                )
            )
        )
        OR
        (
            %(direction)s = 'any' AND (
                (sender = %(id1)s AND receiver = %(id2)s)
                OR (sender = %(id2)s AND receiver = %(id1)s)
                OR (
                    sen_id IN (
                        SELECT u_id FROM user_data
                        WHERE email = %(id1)s OR phone = %(id1)s OR wa_num = %(id1)s
                    )
                    AND rec_id IN (
                        SELECT u_id FROM user_data
                        WHERE email = %(id2)s OR phone = %(id2)s OR wa_num = %(id2)s
                    )
                )
                OR (
                    sen_id IN (
                        SELECT u_id FROM user_data
                        WHERE email = %(id2)s OR phone = %(id2)s OR wa_num = %(id2)s
                    )
                    AND rec_id IN (
                        SELECT u_id FROM user_data
                        WHERE email = %(id1)s OR phone = %(id1)s OR wa_num = %(id1)s
                    )
                )
            )
        )
      )
ORDER BY timestamp {order}
LIMIT %(limit)s OFFSET %(offset)s
"""


class ConversationService:
    def __init__(self, ch_client=None):
        # 👇 inject client (for testing or DI)
        self.client = ch_client or get_ch_client()

    # ======================================
    # CACHE KEY
    # ======================================
    def _make_cache_key(
        self,
        id_1: str,
        id_2: str,
        channels: List[str],
        direction: str,
        from_date: Optional[datetime],
        to_date: Optional[datetime],
        order: str,
        limit: int,
        offset: int,
    ):
        sorted_ids = tuple(sorted([id_1, id_2]))
        sorted_channels = tuple(sorted(channels))

        raw = f"{sorted_ids}:{sorted_channels}:{direction}:{from_date}:{to_date}:{order}:{limit}:{offset}"
        return f"conv:{hashlib.md5(raw.encode()).hexdigest()}"

    # ======================================
    # CACHE GET
    # ======================================
    def _cache_get(self, key: str):
        try:
            data = redis_client.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            log.warning(f"Cache GET failed: {e}")
            return None

    # ======================================
    # CACHE SET
    # ======================================
    def _cache_set(self, key: str, value: List[dict]):
        try:
            serializable = []
            for item in value:
                item_copy = item.copy()
                if isinstance(item_copy.get("timestamp"), datetime):
                    item_copy["timestamp"] = item_copy["timestamp"].isoformat()
                serializable.append(item_copy)

            redis_client.setex(key, CACHE_TTL, json.dumps(serializable))
        except Exception as e:
            log.warning(f"Cache SET failed: {e}")

    # ======================================
    # MAIN METHOD
    # ======================================
    def get_conversation(
        self,
        id_1: str,
        id_2: str,
        channels: List[str],
        direction: str,
        from_date: datetime | None,
        to_date: datetime | None,
        order: str,
        limit: int = 50,
        offset: int = 0,
    ):
        request_id = str(uuid.uuid4())[:8]
        service_start = time.perf_counter()
        conversation_requests_total.inc()

        use_cache = from_date is None and to_date is None

        if use_cache:
            cache_key = self._make_cache_key(
                id_1, id_2, channels, direction, from_date, to_date, order, limit, offset
            )
            cached = self._cache_get(cache_key)

            if cached:
                for item in cached:
                    if isinstance(item.get("timestamp"), str):
                        item["timestamp"] = datetime.fromisoformat(item["timestamp"])

                total_ms = (time.perf_counter() - service_start) * 1000
                conversation_duration_ms.observe(total_ms)
                conversation_rows_returned.observe(len(cached))

                log.info(f"[{request_id}] CACHE HIT rows={len(cached)}")
                return cached

        try:
            sql = BASE_QUERY.format(order="ASC" if order == "asc" else "DESC")

            params = {
                "channels": tuple(channels),  # REQUIRED for IN
                "from_date": from_date,
                "to_date": to_date,
                "direction": direction,
                "id1": id_1,
                "id2": id_2,
                "limit": limit,
                "offset": offset,
            }

            t_execute = time.perf_counter()
            result = self.client.query(sql, parameters=params)
            rows = result.result_rows
            db_query_ms = (time.perf_counter() - t_execute) * 1000

            activities = [
                {
                    "activity_id": str(r[0]),
                    "channel": r[1],
                    "sender": r[2],
                    "receiver": r[3],
                    "content": r[4] or "",
                    "timestamp": r[5],
                }
                for r in rows
            ]

            if use_cache:
                self._cache_set(cache_key, activities)

            total_ms = (time.perf_counter() - service_start) * 1000
            conversation_db_query_duration_ms.observe(db_query_ms)
            conversation_duration_ms.observe(total_ms)
            conversation_rows_returned.observe(len(activities))

            log.info(
                f"[{request_id}] CACHE MISS "
                f"db_query_ms={round(db_query_ms,2)} "
                f"rows={len(activities)}"
            )

            return activities

        except Exception as e:
            conversation_failures_total.inc()
            log.exception(f"[{request_id}] FAILURE {e}")
            raise