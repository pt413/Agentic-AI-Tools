import time
import uuid
import json
import hashlib
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import text

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

_COMBINED_SQL_ASC = text("""
    WITH user_a AS (
        SELECT u_id FROM user_data 
        WHERE email = :id1 OR phone = :id1 OR wa_num = :id1
    ),
    user_b AS (
        SELECT u_id FROM user_data 
        WHERE email = :id2 OR phone = :id2 OR wa_num = :id2
    )
    SELECT
        activity_id,
        channel,
        sender,
        receiver,
        content,
        timestamp
    FROM uni_activity
    WHERE channel = ANY(:channels)
      AND (:from_date IS NULL OR timestamp >= :from_date)
      AND (:to_date IS NULL OR timestamp <= :to_date)
      AND (
        (:direction = 'sent' AND (
            (sender = :id1 AND receiver = :id2)
            OR (sen_id IN (SELECT u_id FROM user_a) AND rec_id IN (SELECT u_id FROM user_b))
        ))
        OR (:direction = 'received' AND (
            (sender = :id2 AND receiver = :id1)
            OR (sen_id IN (SELECT u_id FROM user_b) AND rec_id IN (SELECT u_id FROM user_a))
        ))
        OR (:direction = 'any' AND (
            (sender = :id1 AND receiver = :id2)
            OR (sender = :id2 AND receiver = :id1)
            OR (sen_id IN (SELECT u_id FROM user_a) AND rec_id IN (SELECT u_id FROM user_b))
            OR (sen_id IN (SELECT u_id FROM user_b) AND rec_id IN (SELECT u_id FROM user_a))
        ))
      )
    ORDER BY timestamp ASC
    LIMIT :limit OFFSET :offset
""")

_COMBINED_SQL_DESC = text("""
    WITH user_a AS (
        SELECT u_id FROM user_data 
        WHERE email = :id1 OR phone = :id1 OR wa_num = :id1
    ),
    user_b AS (
        SELECT u_id FROM user_data 
        WHERE email = :id2 OR phone = :id2 OR wa_num = :id2
    )
    SELECT
        activity_id,
        channel,
        sender,
        receiver,
        content,
        timestamp
    FROM uni_activity
    WHERE channel = ANY(:channels)
      AND (:from_date IS NULL OR timestamp >= :from_date)
      AND (:to_date IS NULL OR timestamp <= :to_date)
      AND (
        (:direction = 'sent' AND (
            (sender = :id1 AND receiver = :id2)
            OR (sen_id IN (SELECT u_id FROM user_a) AND rec_id IN (SELECT u_id FROM user_b))
        ))
        OR (:direction = 'received' AND (
            (sender = :id2 AND receiver = :id1)
            OR (sen_id IN (SELECT u_id FROM user_b) AND rec_id IN (SELECT u_id FROM user_a))
        ))
        OR (:direction = 'any' AND (
            (sender = :id1 AND receiver = :id2)
            OR (sender = :id2 AND receiver = :id1)
            OR (sen_id IN (SELECT u_id FROM user_a) AND rec_id IN (SELECT u_id FROM user_b))
            OR (sen_id IN (SELECT u_id FROM user_b) AND rec_id IN (SELECT u_id FROM user_a))
        ))
      )
    ORDER BY timestamp DESC
    LIMIT :limit OFFSET :offset
""")

_SINGLE_USER_SQL = text("""
WITH user_x AS (
    SELECT u_id FROM user_data
    WHERE email = :identifier OR phone = :identifier OR wa_num = :identifier
)
SELECT
    activity_id,
    channel,
    sender,
    receiver,
    timestamp
FROM uni_activity
WHERE channel = ANY(:channels)
  AND (:from_date IS NULL OR timestamp >= :from_date)
  AND (:to_date IS NULL OR timestamp <= :to_date)
  AND (
        sender = :identifier
     OR receiver = :identifier
     OR EXISTS (
        SELECT 1 FROM user_x WHERE user_x.u_id = uni_activity.sen_id
    )
    OR EXISTS (
        SELECT 1 FROM user_x WHERE user_x.u_id = uni_activity.rec_id
    )
  )
ORDER BY timestamp DESC
""")


class ConversationService:
    def __init__(self, db: Session):
        self.db = db

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
    ) -> str:
        """Create deterministic cache key"""
        
        sorted_ids = tuple(sorted([id_1, id_2]))
        sorted_channels = tuple(sorted(channels))
        
        raw = f"{sorted_ids}:{sorted_channels}:{direction}:{from_date}:{to_date}:{order}:{limit}:{offset}"
        return f"conv:{hashlib.md5(raw.encode()).hexdigest()}"

    def _cache_get(self, key: str) -> Optional[List[dict]]:
        """Try to get from cache"""
        try:
            data = redis_client.get(key)
            if data:
                return json.loads(data)
        except Exception as e:
            log.warning(f"Cache GET failed: {e}")
        return None

    def _cache_set(self, key: str, value: List[dict]):
        """Try to set cache"""
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
            
            t_cache = time.perf_counter()
            cached = self._cache_get(cache_key)
            cache_ms = (time.perf_counter() - t_cache) * 1000
            
            if cached:
                
                for item in cached:
                    if isinstance(item.get("timestamp"), str):
                        item["timestamp"] = datetime.fromisoformat(item["timestamp"])
                
                total_ms = (time.perf_counter() - service_start) * 1000
                conversation_duration_ms.observe(total_ms)
                conversation_rows_returned.observe(len(cached))
                
                log.info(
                    f"[{request_id}] CACHE HIT | "
                    f"cache_ms={round(cache_ms, 2)} "
                    f"total_ms={round(total_ms, 2)} "
                    f"rows={len(cached)}"
                )
                return cached

        
        try:
            sql = _COMBINED_SQL_ASC if order == "asc" else _COMBINED_SQL_DESC

            params = {
                "channels": channels,
                "from_date": from_date,
                "to_date": to_date,
                "direction": direction,
                "id1": id_1,
                "id2": id_2,
                "limit": limit,
                "offset": offset,
            }

            t_execute = time.perf_counter()
            result = self.db.execute(sql, params)
            rows = result.fetchall()
            db_query_ms = (time.perf_counter() - t_execute) * 1000

            
            activities = [
                {
                    "activity_id": str(row[0]),
                    "channel": row[1],
                    "sender": row[2],
                    "receiver": row[3],
                    "content": row[4] or "",
                    "timestamp": row[5],
                }
                for row in rows
            ]

            
            if use_cache:
                t_cache_set = time.perf_counter()
                self._cache_set(cache_key, activities)
                cache_set_ms = (time.perf_counter() - t_cache_set) * 1000
            else:
                cache_set_ms = 0

            total_ms = (time.perf_counter() - service_start) * 1000

            conversation_db_query_duration_ms.observe(db_query_ms)
            conversation_duration_ms.observe(total_ms)
            conversation_rows_returned.observe(len(activities))

            log.info(
                f"[{request_id}] CACHE MISS | "
                f"db_query_ms={round(db_query_ms, 2)} "
                f"cache_set_ms={round(cache_set_ms, 2)} "
                f"total_ms={round(total_ms, 2)} "
                f"rows={len(activities)}"
            )

            return activities

        except Exception as e:
            conversation_failures_total.inc()
            error_ms = (time.perf_counter() - service_start) * 1000
            log.exception(
                f"[{request_id}] FAILURE | "
                f"error_at_ms={round(error_ms, 2)} "
                f"error={e}"
            )
            raise

    def get_user_activity(
        self,
        identifier: str,
        channels: List[str],
        from_date: datetime | None,
        to_date: datetime | None,
    ):

        result = self.db.execute(
            _SINGLE_USER_SQL,
            {
                "identifier": identifier,
                "channels": channels,
                "from_date": from_date,
                "to_date": to_date,
            },
        )

        rows = result.fetchall()
        print(identifier, channels)

        return [
            {
                "activity_id": str(r[0]),
                "channel": r[1],
                "sender": r[2],
                "receiver": r[3],
                "timestamp": r[4],
            }
            for r in rows
        ]