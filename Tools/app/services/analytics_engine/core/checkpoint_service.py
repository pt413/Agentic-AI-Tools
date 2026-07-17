import json
from typing import Any

from sqlalchemy import text

from .config import PROCESSOR_CHECKPOINT


class ProcessorCheckpointService:
    def __init__(self, db):
        self.db = db

    def _sanitize_last_id(self, last_id):
        if last_id is None or last_id == "":
            return None
        if isinstance(last_id, int):
            return last_id
        text_value = str(last_id).strip()
        if text_value.isdigit() or (text_value.startswith("-") and text_value[1:].isdigit()):
            try:
                return int(text_value)
            except ValueError:
                return None
        return None

    def _parse_notes_payload(self, notes: Any) -> dict:
        if notes is None:
            return {}
        if isinstance(notes, dict):
            return dict(notes)

        text_value = str(notes).strip()
        if not text_value:
            return {}

        try:
            loaded = json.loads(text_value)
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            pass

        return {"message": text_value}

    def _serialize_notes_payload(self, payload: dict | None) -> str | None:
        clean: dict[str, Any] = {}
        for key, value in (payload or {}).items():
            if value in (None, "", [], {}):
                continue
            clean[str(key)] = value
        if not clean:
            return None
        return json.dumps(clean, default=str, ensure_ascii=False)

    def _merge_notes_payload(self, existing_notes: Any, *, notes: Any = None, last_id: Any = None) -> str | None:
        payload = self._parse_notes_payload(existing_notes)

        if isinstance(notes, dict):
            for key, value in notes.items():
                if value in (None, "", [], {}):
                    payload.pop(str(key), None)
                else:
                    payload[str(key)] = value
        elif notes is not None:
            note_text = str(notes).strip()
            if note_text:
                payload["message"] = note_text

        sanitized_last_id = self._sanitize_last_id(last_id)
        if last_id not in (None, "") and sanitized_last_id is None:
            payload["cursor_source_id_text"] = str(last_id).strip()
        elif sanitized_last_id is not None:
            payload.pop("cursor_source_id_text", None)

        return self._serialize_notes_payload(payload)

    def get_checkpoint(self, processor_name: str) -> dict:
        row = self.db.execute(
            text(
                f"""
                SELECT processor_name, source_table, cursor_mode, last_id, last_timestamp,
                       updated_at, last_batch_count, last_status, last_error, notes
                FROM {PROCESSOR_CHECKPOINT}
                WHERE processor_name = :processor_name
                """
            ),
            {"processor_name": processor_name},
        ).mappings().fetchone()

        data = dict(row) if row else {}
        if not data:
            return {}

        notes_payload = self._parse_notes_payload(data.get("notes"))
        data["notes_payload"] = notes_payload

        last_source_id_text = notes_payload.get("cursor_source_id_text")
        if last_source_id_text not in (None, ""):
            data["last_source_id_text"] = str(last_source_id_text)

        notes_message = notes_payload.get("message")
        if notes_message not in (None, ""):
            data["notes_message"] = str(notes_message)

        return data

    def ensure_checkpoint(self, processor_name: str, source_table: str, cursor_mode: str = "id"):
        self.db.execute(
            text(
                f"""
                INSERT INTO {PROCESSOR_CHECKPOINT}
                (processor_name, source_table, cursor_mode, last_status)
                VALUES (:processor_name, :source_table, :cursor_mode, 'IDLE')
                ON CONFLICT (processor_name) DO NOTHING
                """
            ),
            {
                "processor_name": processor_name,
                "source_table": source_table,
                "cursor_mode": cursor_mode,
            },
        )

    def mark_running(self, processor_name: str, source_table: str):
        self.ensure_checkpoint(processor_name, source_table)
        self.db.execute(
            text(
                f"""
                UPDATE {PROCESSOR_CHECKPOINT}
                SET source_table = :source_table,
                    last_status = 'RUNNING',
                    last_error = NULL,
                    updated_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
                WHERE processor_name = :processor_name
                """
            ),
            {"processor_name": processor_name, "source_table": source_table},
        )

    def mark_success(
        self,
        processor_name: str,
        source_table: str,
        last_id=None,
        last_timestamp=None,
        last_batch_count: int = 0,
        notes: str | dict | None = None,
    ):
        self.ensure_checkpoint(processor_name, source_table)
        current = self.get_checkpoint(processor_name)
        merged_notes = self._merge_notes_payload(
            current.get("notes"),
            notes=notes,
            last_id=last_id,
        )
        self.db.execute(
            text(
                f"""
                UPDATE {PROCESSOR_CHECKPOINT}
                SET source_table = :source_table,
                    last_id = COALESCE(:last_id, last_id),
                    last_timestamp = COALESCE(:last_timestamp, last_timestamp),
                    last_batch_count = :last_batch_count,
                    last_status = 'IDLE',
                    last_error = NULL,
                    notes = :notes,
                    updated_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
                WHERE processor_name = :processor_name
                """
            ),
            {
                "processor_name": processor_name,
                "source_table": source_table,
                "last_id": self._sanitize_last_id(last_id),
                "last_timestamp": last_timestamp,
                "last_batch_count": int(last_batch_count or 0),
                "notes": merged_notes,
            },
        )

    def mark_failed(
        self,
        processor_name: str,
        source_table: str,
        error_message: str,
        last_id=None,
        last_timestamp=None,
        notes: str | dict | None = None,
    ):
        self.ensure_checkpoint(processor_name, source_table)
        current = self.get_checkpoint(processor_name)
        merged_notes = self._merge_notes_payload(
            current.get("notes"),
            notes=notes,
            last_id=last_id,
        )
        self.db.execute(
            text(
                f"""
                UPDATE {PROCESSOR_CHECKPOINT}
                SET source_table = :source_table,
                    last_id = COALESCE(:last_id, last_id),
                    last_timestamp = COALESCE(:last_timestamp, last_timestamp),
                    last_status = 'FAILED',
                    last_error = :error_message,
                    notes = :notes,
                    updated_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
                WHERE processor_name = :processor_name
                """
            ),
            {
                "processor_name": processor_name,
                "source_table": source_table,
                "last_id": self._sanitize_last_id(last_id),
                "last_timestamp": last_timestamp,
                "error_message": error_message[:4000] if error_message else None,
                "notes": merged_notes,
            },
        )
