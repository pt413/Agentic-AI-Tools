from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from ..core.config import CUSTOMER_CURRENT_STATE, EVENT_CONTEXT, EVENT_FACT, EVENT_PARTICIPANT, SCHEMA_NAME


class CurrentStateProjectionService:
    """
    Builds a rolling "current customer state" projection per anchor.

    Preferred anchors:
      1) person_id from event_participant
      2) lead / booking / user contexts from event_context
    """

    _STAGE_ORDER = {
        "new_or_unqualified": 10,
        "engaged_discovery": 20,
        "active_evaluation": 30,
        "decision_pending": 40,
        "commitment_in_progress": 50,
        "onboarding_or_activation": 60,
        "active_service_early": 70,
        "active_service_steady": 80,
        "issue_recovery": 85,
        "renewal_or_expansion": 90,
        "exit_or_offboarding": 95,
        "inactive_or_lost": 100,
    }
    _HEALTH_ORDER = {
        "healthy": 10,
        "stable": 20,
        "watchlist": 30,
        "at_risk": 40,
        "critical": 50,
        "recovering": 25,
    }
    _RESOLUTION_ORDER = {
        "new": 10,
        "awaiting_business": 20,
        "awaiting_customer": 30,
        "in_progress": 40,
        "resolved": 50,
        "closed": 60,
        "reopened": 70,
    }

    def __init__(self, db, ontology_service=None):
        self.db = db
        self.ontology = ontology_service
        self._batch_cache: dict[int, dict[str, Any]] = {}
        self._table_exists_cache: dict[str, bool] = {}
        self._sql_event = text(
            f"""
            SELECT
                event_id,
                event_name,
                event_family,
                event_channel,
                event_direction,
                event_time,
                event_status,
                event_meta,
                source_table,
                source_id
            FROM {EVENT_FACT}
            WHERE event_id = :event_id
            """
        )
        self._sql_participants = text(
            f"""
            SELECT DISTINCT person_id
            FROM {EVENT_PARTICIPANT}
            WHERE event_id = :event_id
              AND person_id IS NOT NULL
            """
        )
        self._sql_contexts = text(
            f"""
            SELECT context_type, context_value
            FROM {EVENT_CONTEXT}
            WHERE event_id = :event_id
            """
        )
        self._sql_load_state = text(
            f"""
            SELECT *
            FROM {CUSTOMER_CURRENT_STATE}
            WHERE anchor_type = :anchor_type
              AND anchor_id = :anchor_id
            """
        )
        self._sql_upsert_state = text(
            f"""
            INSERT INTO {CUSTOMER_CURRENT_STATE}
            (
                anchor_type,
                anchor_id,
                person_id,
                lead_id,
                booking_id,
                user_id,
                journey_stage,
                resolution_stage,
                relationship_health,
                ownership_team,
                next_best_action,
                last_event_id,
                last_event_time,
                state_meta
            )
            VALUES
            (
                :anchor_type,
                :anchor_id,
                :person_id,
                :lead_id,
                :booking_id,
                :user_id,
                :journey_stage,
                :resolution_stage,
                :relationship_health,
                :ownership_team,
                :next_best_action,
                :last_event_id,
                :last_event_time,
                CAST(:state_meta AS JSONB)
            )
            ON CONFLICT (anchor_type, anchor_id)
            DO UPDATE SET
                person_id = COALESCE(EXCLUDED.person_id, {CUSTOMER_CURRENT_STATE}.person_id),
                lead_id = COALESCE(EXCLUDED.lead_id, {CUSTOMER_CURRENT_STATE}.lead_id),
                booking_id = COALESCE(EXCLUDED.booking_id, {CUSTOMER_CURRENT_STATE}.booking_id),
                user_id = COALESCE(EXCLUDED.user_id, {CUSTOMER_CURRENT_STATE}.user_id),
                journey_stage = COALESCE(EXCLUDED.journey_stage, {CUSTOMER_CURRENT_STATE}.journey_stage),
                resolution_stage = COALESCE(EXCLUDED.resolution_stage, {CUSTOMER_CURRENT_STATE}.resolution_stage),
                relationship_health = COALESCE(EXCLUDED.relationship_health, {CUSTOMER_CURRENT_STATE}.relationship_health),
                ownership_team = COALESCE(EXCLUDED.ownership_team, {CUSTOMER_CURRENT_STATE}.ownership_team),
                next_best_action = COALESCE(EXCLUDED.next_best_action, {CUSTOMER_CURRENT_STATE}.next_best_action),
                last_event_id = COALESCE(EXCLUDED.last_event_id, {CUSTOMER_CURRENT_STATE}.last_event_id),
                last_event_time = COALESCE(EXCLUDED.last_event_time, {CUSTOMER_CURRENT_STATE}.last_event_time),
                state_meta = COALESCE(EXCLUDED.state_meta, {CUSTOMER_CURRENT_STATE}.state_meta),
                updated_at = NOW()
            """
        )

    def reset_batch_cache(self):
        self._batch_cache = {}

    def _table_exists(self, table_name: str) -> bool:
        if table_name in self._table_exists_cache:
            return self._table_exists_cache[table_name]

        row = self.db.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = :schema_name
                      AND table_name = :table_name
                ) AS present
                """
            ),
            {
                "schema_name": SCHEMA_NAME,
                "table_name": table_name,
            },
        ).mappings().fetchone()
        present = bool(row["present"]) if row else False
        self._table_exists_cache[table_name] = present
        return present

    def _meta_dict(self, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
                return decoded if isinstance(decoded, dict) else {}
            except Exception:
                return {}
        try:
            return dict(value)
        except Exception:
            return {}

    def _load_event(self, event_id: int) -> dict[str, Any] | None:
        row = self.db.execute(self._sql_event, {"event_id": int(event_id)}).mappings().fetchone()
        return dict(row) if row else None

    def _load_tags(self, event_id: int) -> dict[str, str]:
        if self.ontology:
            result = self.ontology.tag_event(int(event_id)) or {}
            tags = result.get("tags")
            if isinstance(tags, dict):
                return tags
        event_row = self._load_event(int(event_id))
        if not event_row:
            return {}
        meta = self._meta_dict(event_row.get("event_meta"))
        ontology_meta = self._meta_dict(meta.get("ontology"))
        tags = ontology_meta.get("tags")
        return tags if isinstance(tags, dict) else {}

    def _load_anchors(self, event_id: int) -> list[dict[str, Any]]:
        anchors: list[dict[str, Any]] = []

        participant_rows = self.db.execute(self._sql_participants, {"event_id": int(event_id)}).mappings().fetchall()
        for row in participant_rows:
            person_id = row.get("person_id")
            if person_id is not None:
                anchors.append({
                    "anchor_type": "person",
                    "anchor_id": str(person_id),
                    "person_id": int(person_id),
                    "lead_id": None,
                    "booking_id": None,
                    "user_id": None,
                })

        context_rows = self.db.execute(self._sql_contexts, {"event_id": int(event_id)}).mappings().fetchall()
        for row in context_rows:
            context_type = str(row.get("context_type") or "").lower()
            context_value = row.get("context_value")
            if context_value in (None, ""):
                continue
            if context_type in {"lead", "booking", "user"}:
                anchors.append({
                    "anchor_type": context_type,
                    "anchor_id": str(context_value),
                    "person_id": None,
                    "lead_id": str(context_value) if context_type == "lead" else None,
                    "booking_id": str(context_value) if context_type == "booking" else None,
                    "user_id": str(context_value) if context_type == "user" else None,
                })

        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for anchor in anchors:
            key = (anchor["anchor_type"], anchor["anchor_id"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(anchor)
        return deduped

    def _prefer_value(self, current: str | None, new: str | None, ranking: dict[str, int] | None = None) -> str | None:
        if not new:
            return current
        if not current:
            return new
        if not ranking:
            return new
        return new if ranking.get(new, -1) >= ranking.get(current, -1) else current

    def _build_state_meta(self, event_row: dict[str, Any], tags: dict[str, str], anchor: dict[str, Any]) -> dict[str, Any]:
        event_meta = self._meta_dict(event_row.get("event_meta"))
        return {
            "last_event_name": event_row.get("event_name"),
            "last_event_family": event_row.get("event_family"),
            "last_event_channel": event_row.get("event_channel"),
            "last_event_direction": event_row.get("event_direction"),
            "last_event_status": event_row.get("event_status"),
            "last_source_table": event_row.get("source_table"),
            "last_source_id": event_row.get("source_id"),
            "anchor_type": anchor.get("anchor_type"),
            "derived_tags": tags,
            "event_meta_hint": {
                key: event_meta.get(key)
                for key in (
                    "subject", "category", "priority", "booking_id", "lead_id", "user_id",
                    "property_id", "ticket_rating", "rms_rating", "stay_rating",
                )
                if key in event_meta
            },
        }

    def apply_event(self, event_id: int) -> dict[str, Any]:
        event_id = int(event_id)
        if event_id in self._batch_cache:
            return self._batch_cache[event_id]

        if not self._table_exists("customer_current_state"):
            result = {"rows_written": 0}
            self._batch_cache[event_id] = result
            return result

        event_row = self._load_event(event_id)
        if not event_row:
            result = {"rows_written": 0}
            self._batch_cache[event_id] = result
            return result

        tags = self._load_tags(event_id)
        anchors = self._load_anchors(event_id)
        if not anchors:
            result = {"rows_written": 0}
            self._batch_cache[event_id] = result
            return result

        rows_written = 0
        for anchor in anchors:
            existing = self.db.execute(
                self._sql_load_state,
                {
                    "anchor_type": anchor["anchor_type"],
                    "anchor_id": anchor["anchor_id"],
                },
            ).mappings().fetchone()
            existing = dict(existing) if existing else {}

            journey_stage = self._prefer_value(existing.get("journey_stage"), tags.get("journey_stage"), self._STAGE_ORDER)
            resolution_stage = self._prefer_value(existing.get("resolution_stage"), tags.get("resolution_stage"), self._RESOLUTION_ORDER)
            relationship_health = self._prefer_value(existing.get("relationship_health"), tags.get("relationship_health"), self._HEALTH_ORDER)
            ownership_team = tags.get("ownership_team") or existing.get("ownership_team")
            next_best_action = tags.get("next_best_action") or existing.get("next_best_action")

            if anchor["anchor_type"] == "person":
                person_id = anchor["person_id"]
                lead_id = existing.get("lead_id")
                booking_id = existing.get("booking_id")
                user_id = existing.get("user_id")
            else:
                person_id = existing.get("person_id")
                lead_id = anchor.get("lead_id") or existing.get("lead_id")
                booking_id = anchor.get("booking_id") or existing.get("booking_id")
                user_id = anchor.get("user_id") or existing.get("user_id")

            state_meta = self._build_state_meta(event_row, tags, anchor)

            self.db.execute(
                self._sql_upsert_state,
                {
                    "anchor_type": anchor["anchor_type"],
                    "anchor_id": anchor["anchor_id"],
                    "person_id": person_id,
                    "lead_id": lead_id,
                    "booking_id": booking_id,
                    "user_id": user_id,
                    "journey_stage": journey_stage,
                    "resolution_stage": resolution_stage,
                    "relationship_health": relationship_health,
                    "ownership_team": ownership_team,
                    "next_best_action": next_best_action,
                    "last_event_id": event_row.get("event_id"),
                    "last_event_time": event_row.get("event_time"),
                    "state_meta": json.dumps(state_meta, default=str),
                },
            )
            rows_written += 1

        result = {"rows_written": rows_written}
        self._batch_cache[event_id] = result
        return result
