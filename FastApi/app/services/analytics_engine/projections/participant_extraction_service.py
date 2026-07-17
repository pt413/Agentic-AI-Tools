from sqlalchemy import text

from ..core.config import EVENT_PARTICIPANT


class ParticipantExtractionService:
    def __init__(self, db, identity_service):
        self.db = db
        self.identity = identity_service
        self._batch_cache = {}
        self._sql_upsert = text(f"""
            INSERT INTO {EVENT_PARTICIPANT}
            (
                event_id,
                participant_seq,
                person_id,
                participant_role,
                direction_role,
                raw_key_type,
                raw_key_value,
                raw_label,
                resolution_method,
                resolved_at
            )
            VALUES
            (
                :eid,
                :seq,
                :pid,
                :role,
                :drole,
                :kt,
                :kv,
                :rlabel,
                'exact_key',
                NOW()
            )
            ON CONFLICT (event_id, participant_seq)
            DO UPDATE
            SET
                person_id = COALESCE(EXCLUDED.person_id, {EVENT_PARTICIPANT}.person_id),
                participant_role = COALESCE({EVENT_PARTICIPANT}.participant_role, EXCLUDED.participant_role),
                direction_role = COALESCE({EVENT_PARTICIPANT}.direction_role, EXCLUDED.direction_role),
                raw_key_type = COALESCE({EVENT_PARTICIPANT}.raw_key_type, EXCLUDED.raw_key_type),
                raw_key_value = COALESCE({EVENT_PARTICIPANT}.raw_key_value, EXCLUDED.raw_key_value),
                raw_label = COALESCE({EVENT_PARTICIPANT}.raw_label, EXCLUDED.raw_label),
                resolution_method = COALESCE({EVENT_PARTICIPANT}.resolution_method, EXCLUDED.resolution_method),
                resolved_at = COALESCE({EVENT_PARTICIPANT}.resolved_at, EXCLUDED.resolved_at)
            RETURNING event_participant_id
        """)

    def reset_batch_cache(self):
        self._batch_cache = {}

    def add_participant(
        self,
        event_id: int,
        participant_seq: int,
        key_type: str,
        key_value: str,
        role: str,
        direction_role: str | None = None,
        raw_label: str | None = None,
        source_table: str = "staging_call_log_unified",
        source_id: str | int | None = None,
        event_time=None,
    ) -> int | None:
        if not key_type or not key_value:
            return None

        resolved_source_id = (
            str(source_id)
            if source_id is not None
            else f"{event_id}:{participant_seq}:{key_type}:{key_value}"
        )

        cache_key = (int(event_id), int(participant_seq), str(key_type), str(key_value))
        if cache_key in self._batch_cache:
            return self._batch_cache[cache_key]

        person_id = self.identity.resolve_or_create_person(
            key_type=key_type,
            key_value=key_value,
            source_table=source_table,
            source_id=resolved_source_id,
            event_time=event_time,
        )

        if person_id is None:
            raise RuntimeError(
                f"Failed to resolve/create person for "
                f"event_id={event_id}, participant_seq={participant_seq}, "
                f"key_type={key_type}, key_value={key_value}"
            )

        row = self.db.execute(
            self._sql_upsert,
            {
                "eid": event_id,
                "seq": participant_seq,
                "pid": person_id,
                "role": role,
                "drole": direction_role,
                "kt": key_type,
                "kv": str(key_value),
                "rlabel": raw_label,
            },
        ).fetchone()

        event_participant_id = row[0] if row else None
        self._batch_cache[cache_key] = event_participant_id
        return event_participant_id
