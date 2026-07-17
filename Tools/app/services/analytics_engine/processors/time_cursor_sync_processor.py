from __future__ import annotations

from typing import Any

from .base_sync_processor import BaseSyncProcessor


class TimeCursorSyncProcessor(BaseSyncProcessor):
    """
    Base processor for sources whose checkpoint should advance by timestamp
    (and optionally a non-numeric source_id), not only by integer source_id.
    """

    def get_checkpoint_last_id(self, start_source_id=None):
        checkpoint = self.checkpoints.get_checkpoint(self.processor_name)
        if start_source_id is not None:
            return start_source_id

        if checkpoint.get("last_source_id_text") not in (None, ""):
            return checkpoint.get("last_source_id_text")
        return checkpoint.get("last_id")

    def get_checkpoint_last_timestamp(self):
        checkpoint = self.checkpoints.get_checkpoint(self.processor_name)
        return checkpoint.get("last_timestamp")

    def fetch_rows(self, last_source_id: Any, batch_size: int):
        raise NotImplementedError

    def fetch_rows_since(self, last_timestamp, last_source_id, batch_size: int):
        raise NotImplementedError

    def fetch_rows_with_time_cursor(self, last_source_id: Any, last_timestamp, batch_size: int):
        subclass_impl = type(self).fetch_rows_since
        if subclass_impl is not TimeCursorSyncProcessor.fetch_rows_since:
            return self.fetch_rows_since(
                last_timestamp=last_timestamp,
                last_source_id=last_source_id,
                batch_size=batch_size,
            )
        raise NotImplementedError

    def get_row_checkpoint_id(self, row, row_result: dict[str, Any] | None = None):
        if row_result and "last_source_id" in row_result:
            return row_result["last_source_id"]
        return getattr(row, "source_id", None)

    def get_row_checkpoint_timestamp(self, row, row_result: dict[str, Any] | None = None):
        if row_result and "last_timestamp" in row_result:
            return row_result["last_timestamp"]
        if hasattr(self, "get_row_cursor_timestamp"):
            return self.get_row_cursor_timestamp(row, row_result=row_result)
        return super().get_row_checkpoint_timestamp(row, row_result=row_result)

    def run(self, batch_size=None, limit=None, start_source_id=None) -> dict[str, Any]:
        batch_size = int(batch_size or self.default_batch_size)
        last_source_id = self.get_checkpoint_last_id(start_source_id=start_source_id)
        last_timestamp = self.get_checkpoint_last_timestamp()

        self.checkpoints.mark_running(self.processor_name, self.source_table)
        self.db.commit()

        self.before_run(
            batch_size=batch_size,
            limit=limit,
            start_source_id=start_source_id,
            last_source_id=last_source_id,
        )

        processed = 0
        aggregates: dict[str, Any] = {}
        last_checkpoint_timestamp = last_timestamp
        last_checkpoint_id = last_source_id
        wrote_success_checkpoint = False
        last_committed_id = last_source_id
        last_committed_timestamp = last_timestamp

        try:
            while True:
                remaining = None if limit is None else int(limit) - processed
                if remaining is not None and remaining <= 0:
                    break

                current_batch_size = min(batch_size, remaining) if remaining else batch_size
                rows = self.fetch_rows_with_time_cursor(
                    last_source_id=last_checkpoint_id,
                    last_timestamp=last_checkpoint_timestamp,
                    batch_size=current_batch_size,
                )
                if not rows:
                    break

                self.reset_batch_state()

                for row in rows:
                    row_result = self.process_row(row) or {}

                    row_last_source_id = self.get_row_checkpoint_id(row, row_result=row_result)
                    if row_last_source_id is not None:
                        last_checkpoint_id = row_last_source_id

                    row_checkpoint_timestamp = self.get_row_checkpoint_timestamp(row, row_result=row_result)
                    if row_checkpoint_timestamp is not None:
                        last_checkpoint_timestamp = row_checkpoint_timestamp

                    if row_result.get("processed", True):
                        processed += 1

                    for key, value in row_result.items():
                        if key in ("processed", "last_source_id", "last_timestamp"):
                            continue
                        if isinstance(value, bool):
                            aggregates[key] = int(aggregates.get(key, 0)) + int(value)
                        elif isinstance(value, int):
                            aggregates[key] = int(aggregates.get(key, 0)) + value
                        else:
                            aggregates[key] = value

                self.checkpoints.mark_success(
                    self.processor_name,
                    self.source_table,
                    last_id=last_checkpoint_id,
                    last_timestamp=last_checkpoint_timestamp,
                    last_batch_count=len(rows),
                )
                self.db.commit()
                wrote_success_checkpoint = True
                last_committed_id = last_checkpoint_id
                last_committed_timestamp = last_checkpoint_timestamp
            if not wrote_success_checkpoint:
                self.checkpoints.mark_success(
                    self.processor_name,
                    self.source_table,
                    last_id=last_checkpoint_id,
                    last_timestamp=last_checkpoint_timestamp,
                    last_batch_count=0,
                )
                self.db.commit()

            result = self.build_result(
                processed=processed,
                last_source_id=last_checkpoint_id,
                **aggregates,
            )
            self.after_run(
                result,
                batch_size=batch_size,
                limit=limit,
                start_source_id=start_source_id,
            )
            return result

        except Exception as exc:
            self.db.rollback()
            self.checkpoints.mark_failed(
                self.processor_name,
                self.source_table,
                str(exc),
                last_id=last_committed_id,
                last_timestamp=last_committed_timestamp,
            )
            self.db.commit()
            raise
