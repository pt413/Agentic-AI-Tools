from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseSyncProcessor(ABC):
    """
    Repo-aligned base class for independent source-table sync processors.

    Performance-focused behavior:
    - batch commits only
    - checkpoint writes only once per batch
    - per-batch cache reset hook
    - lightweight aggregate accumulation

    Semantic-layer behavior:
    - captures every event_id created inside process_row()
    - tags ontology on the event
    - projects current-state rows after participants + contexts are written
    """

    PROCESSOR_NAME: str | None = None
    SOURCE_TABLE: str | None = None

    def __init__(self, db, services, default_batch_size: int = 5000):
        self.db = db
        self.services = services
        self.default_batch_size = int(default_batch_size)

        self.identity = services.identity
        self.events = services.events
        self.participants = services.participants
        self.context = services.context
        self.source = services.source
        self.checkpoints = services.checkpoints
        self.lead_facts = getattr(services, "lead_facts", None)
        self.booking_facts = getattr(services, "booking_facts", None)
        self.ontology = getattr(services, "ontology", None)
        self.current_state = getattr(services, "current_state", None)

    @property
    def processor_name(self) -> str:
        if not self.PROCESSOR_NAME:
            raise ValueError("Subclass must define PROCESSOR_NAME")
        return self.PROCESSOR_NAME

    @property
    def source_table(self) -> str:
        if not self.SOURCE_TABLE:
            raise ValueError("Subclass must define SOURCE_TABLE")
        return self.SOURCE_TABLE

    @property
    def source_table_name(self) -> str:
        value = getattr(self, "SOURCE_TABLE_NAME", None)
        if value:
            return value
        return str(self.source_table).split(".")[-1].replace('"', "")

    def get_checkpoint_last_id(self, start_source_id=None) -> int:
        checkpoint = self.checkpoints.get_checkpoint(self.processor_name)
        return int(start_source_id if start_source_id is not None else checkpoint.get("last_id") or 0)

    def before_run(self, **kwargs):
        return None

    def after_run(self, result: dict[str, Any], **kwargs):
        return None

    def reset_batch_state(self):
        if hasattr(self.identity, "reset_batch_cache"):
            self.identity.reset_batch_cache()
        for service_name in (
            "events",
            "participants",
            "context",
            "lead_facts",
            "booking_facts",
            "ontology",
            "current_state",
        ):
            service = getattr(self, service_name, None)
            if service and hasattr(service, "reset_batch_cache"):
                service.reset_batch_cache()

    def get_row_checkpoint_timestamp(self, row, row_result: dict[str, Any] | None = None):
        return getattr(row, "synced_at", None)

    def begin_row_capture(self):
        if hasattr(self.events, "begin_row_capture"):
            self.events.begin_row_capture()

    def consume_captured_event_ids(self):
        if hasattr(self.events, "consume_captured_event_ids"):
            return self.events.consume_captured_event_ids()
        return []

    def _apply_semantics(self, event_ids, row=None, row_result: dict[str, Any] | None = None) -> dict[str, int]:
        aggregates = {
            "ontology_events_tagged": 0,
            "ontology_tags_written": 0,
            "state_rows_written": 0,
        }
        if not event_ids:
            return aggregates

        for event_id in event_ids:
            if self.ontology:
                result = self.ontology.tag_event(
                    event_id=event_id,
                    source_table=self.source_table_name,
                ) or {}
                aggregates["ontology_events_tagged"] += 1 if result.get("tagged", True) else 0
                aggregates["ontology_tags_written"] += int(result.get("tags_written", 0))

            if self.current_state:
                result = self.current_state.apply_event(event_id=event_id) or {}
                aggregates["state_rows_written"] += int(result.get("rows_written", 0))

        return aggregates

    def _merge_row_result(self, row_result: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
        merged = dict(row_result or {})
        for key, value in (extra or {}).items():
            if isinstance(value, bool):
                merged[key] = bool(merged.get(key, False)) or value
            elif isinstance(value, int):
                merged[key] = int(merged.get(key, 0)) + value
            elif value is not None and key not in merged:
                merged[key] = value
        return merged

    @abstractmethod
    def fetch_rows(self, last_source_id: int, batch_size: int):
        raise NotImplementedError

    @abstractmethod
    def process_row(self, row) -> dict[str, Any] | None:
        raise NotImplementedError

    def build_result(self, processed: int, last_source_id: int, **extra) -> dict[str, Any]:
        result = {
            "processor_name": self.processor_name,
            "source_table": self.source_table,
            "processed_rows": int(processed),
            "last_source_id": last_source_id,
        }
        result.update(extra)
        return result

    def run(self, batch_size=None, limit=None, start_source_id=None) -> dict[str, Any]:
        batch_size = int(batch_size or self.default_batch_size)
        last_source_id = self.get_checkpoint_last_id(start_source_id=start_source_id)

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
        last_checkpoint_timestamp = None
        last_committed_source_id = last_source_id
        try:
            while True:
                remaining = None if limit is None else int(limit) - processed
                if remaining is not None and remaining <= 0:
                    break

                current_batch_size = min(batch_size, remaining) if remaining else batch_size
                rows = self.fetch_rows(last_source_id=last_source_id, batch_size=current_batch_size)
                if not rows:
                    break

                self.reset_batch_state()

                for row in rows:
                    self.begin_row_capture()
                    row_result = self.process_row(row) or {}
                    semantic_result = self._apply_semantics(
                        event_ids=self.consume_captured_event_ids(),
                        row=row,
                        row_result=row_result,
                    )
                    row_result = self._merge_row_result(row_result, semantic_result)

                    row_last_source_id = row_result.get("last_source_id", getattr(row, "source_id", None))
                    if row_last_source_id is not None:
                        last_source_id = int(row_last_source_id)

                    row_checkpoint_timestamp = self.get_row_checkpoint_timestamp(row, row_result=row_result)
                    if row_checkpoint_timestamp is not None:
                        last_checkpoint_timestamp = row_checkpoint_timestamp

                    if row_result.get("processed", True):
                        processed += 1

                    for key, value in row_result.items():
                        if key in ("processed", "last_source_id"):
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
                    last_id=last_source_id,
                    last_timestamp=last_checkpoint_timestamp,
                    last_batch_count=len(rows),
                )
                self.db.commit()
                last_committed_source_id = last_source_id
            result = self.build_result(processed=processed, last_source_id=last_source_id, **aggregates)
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
                last_id=last_committed_source_id,
            )
            self.db.commit()
            raise
