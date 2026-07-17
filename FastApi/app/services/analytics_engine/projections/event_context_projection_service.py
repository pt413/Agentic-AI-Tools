from sqlalchemy import text

from ..core.config import EVENT_CONTEXT


class EventContextProjectionService:
    def __init__(self, db):
        self.db = db
        self._batch_cache = set()
        self._sql_upsert = text(
            f"""
            INSERT INTO {EVENT_CONTEXT}
            (event_id, context_type, context_value)
            VALUES
            (:eid, :ct, :cv)
            ON CONFLICT (event_id, context_type, context_value)
            DO UPDATE SET context_value = EXCLUDED.context_value
            RETURNING event_context_id
            """
        )

    def reset_batch_cache(self):
        self._batch_cache = set()

    def add_context(self, event_id, context_type, context_value):
        if not context_value:
            return None

        context_key = (int(event_id), str(context_type), str(context_value))
        if context_key in self._batch_cache:
            return 1

        row = self.db.execute(
            self._sql_upsert,
            {
                "eid": event_id,
                "ct": str(context_type),
                "cv": str(context_value),
            },
        ).fetchone()
        self._batch_cache.add(context_key)
        return row.event_context_id if row else 1
