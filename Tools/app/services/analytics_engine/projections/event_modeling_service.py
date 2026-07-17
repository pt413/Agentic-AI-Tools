import json
from decimal import Decimal

from sqlalchemy import text

from ..core.config import EVENT_FACT


class EventModelingService:
    VALID_EVENT_FAMILIES = {
        "communication",
        "identity",
        "lead",
        "booking",
        "stay",
        "support",
        "finance",
    }

    # Keep this aligned to what event_fact.chk_event_status accepts in Postgres.
    VALID_EVENT_STATUS = {"completed", "missed", "cancelled", "unknown"}
    VALID_METRIC_UNITS = {"seconds", "minutes", "hours", "days", "count", "amount", "rating", "score"}

    METRIC_UNIT_ALIASES = {
        "char": "count",
        "chars": "count",
        "character": "count",
        "characters": "count",
        "length": "count",
        "items": "count",
    }

    EVENT_STATUS_ALIASES = {
        "1": "completed",
        "true": "completed",
        "done": "completed",
        "closed": "completed",
        "resolved": "completed",
        "success": "completed",
        "successful": "completed",
        "complete": "completed",
        "completed": "completed",
        # Transport-specific states are preserved in event_meta by processors,
        # but event_fact.event_status must stay inside the DB enum.
        "read": "completed",
        "sent": "completed",
        "delivered": "completed",
        "cancel": "cancelled",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "abort": "cancelled",
        "aborted": "cancelled",
        "missed": "missed",
        "no_show": "missed",
        "noshow": "missed",
        "unknown": "unknown",
        "na": "unknown",
        "n/a": "unknown",
    }

    EVENT_FAMILY_ALIASES = {
        "intent": "lead",
        "conversion": "booking",
        "ticket": "support",
        "payment": "finance",
        "visit": "lead",
        "site_visit": "lead",
        "web_visit": "lead",
        "wishlist": "lead",
        "cart": "lead",
        "engagement": "lead",
    }

    def __init__(self, db):
        self.db = db
        self._batch_cache = {}
        self._sql_upsert = text(
            f"""
            INSERT INTO {EVENT_FACT}
            (
                event_family,
                event_name,
                event_channel,
                event_direction,
                event_time,
                event_end_time,
                metric_value,
                metric_unit,
                metric_name,
                event_status,
                event_meta,
                source_table,
                source_id
            )
            VALUES
            (
                :family,
                :name,
                :channel,
                :direction,
                :time,
                :end_time,
                :value,
                :unit,
                :metric,
                :status,
                CAST(:meta AS JSONB),
                :st,
                :sid
            )
            ON CONFLICT (source_table, source_id, event_name, event_time)
            DO UPDATE SET
                event_family = EXCLUDED.event_family,
                event_channel = COALESCE(EXCLUDED.event_channel, {EVENT_FACT}.event_channel),
                event_direction = COALESCE(EXCLUDED.event_direction, {EVENT_FACT}.event_direction),
                event_end_time = COALESCE(EXCLUDED.event_end_time, {EVENT_FACT}.event_end_time),
                metric_value = COALESCE(EXCLUDED.metric_value, {EVENT_FACT}.metric_value),
                metric_unit = COALESCE(EXCLUDED.metric_unit, {EVENT_FACT}.metric_unit),
                metric_name = COALESCE(EXCLUDED.metric_name, {EVENT_FACT}.metric_name),
                event_status = COALESCE(EXCLUDED.event_status, {EVENT_FACT}.event_status),
                event_meta = CASE
                    WHEN EXCLUDED.event_meta IS NOT NULL AND EXCLUDED.event_meta <> '{{}}'::jsonb
                    THEN EXCLUDED.event_meta
                    ELSE {EVENT_FACT}.event_meta
                END
            RETURNING event_id
            """
        )

    def reset_batch_cache(self):
        self._batch_cache = {}

    def _coerce_event_family(self, value, event_name=None, event_channel=None):
        if value is not None:
            normalized = str(value).strip().lower()
            if normalized in self.VALID_EVENT_FAMILIES:
                return normalized
            if normalized in self.EVENT_FAMILY_ALIASES:
                return self.EVENT_FAMILY_ALIASES[normalized]

        name = str(event_name or "").lower()
        channel = str(event_channel or "").lower()
        hint = f"{name} {channel}"

        if any(token in hint for token in ("ticket", "complaint", "support", "issue")):
            return "support"
        if any(token in hint for token in ("payment", "invoice", "refund", "amount", "revenue")):
            return "finance"
        if any(token in hint for token in ("booking", "reservation", "confirm")):
            return "booking"
        if any(token in hint for token in ("site_visit", "visit", "wishlist", "web", "cart", "page_view", "lead", "prospect")):
            return "lead"
        if any(token in hint for token in ("checkin", "checkout", "stay")):
            return "stay"
        if any(token in hint for token in ("lead", "prospect")):
            return "lead"
        if any(token in hint for token in ("email", "whatsapp", "call", "message", "sms")):
            return "communication"
        if any(token in hint for token in ("login", "signup", "register", "identity", "account", "user")):
            return "identity"
        return "lead"

    def _coerce_event_status(self, value):
        if value is None:
            return None

        normalized = str(value).strip().lower()
        if not normalized:
            return None

        normalized = self.EVENT_STATUS_ALIASES.get(normalized, normalized)
        if normalized in self.VALID_EVENT_STATUS:
            return normalized

        # Important: do not emit unsupported statuses like 'active', 'inactive',
        # or any raw transport states not covered above. Leave them NULL instead.
        return None

    def _coerce_metric_unit(self, value):
        if value is None:
            return None

        normalized = str(value).strip().lower()
        if not normalized:
            return None

        normalized = self.METRIC_UNIT_ALIASES.get(normalized, normalized)
        return normalized if normalized in self.VALID_METRIC_UNITS else None

    def _coerce_metric_value(self, value):
        if value is None or value == "":
            return None
        if isinstance(value, Decimal):
            return value
        return value

    def create_event(
        self,
        event_family,
        event_name,
        event_channel,
        event_direction,
        event_time,
        source_table,
        source_id,
        metric_value=None,
        metric_unit=None,
        metric_name=None,
        meta=None,
        event_end_time=None,
        event_status=None,
    ):
        cache_key = (str(source_table), str(source_id), str(event_name), event_time)
        cached = self._batch_cache.get(cache_key)
        if cached is not None:
            return cached

        event_family = self._coerce_event_family(event_family, event_name=event_name, event_channel=event_channel)
        event_status = self._coerce_event_status(event_status)
        metric_unit = self._coerce_metric_unit(metric_unit)
        metric_value = self._coerce_metric_value(metric_value)

        meta_json = json.dumps(meta or {}, default=str)
        row = self.db.execute(
            self._sql_upsert,
            {
                "family": event_family,
                "name": event_name,
                "channel": event_channel,
                "direction": event_direction,
                "time": event_time,
                "end_time": event_end_time,
                "value": metric_value,
                "unit": metric_unit,
                "metric": metric_name,
                "status": event_status,
                "meta": meta_json,
                "st": source_table,
                "sid": str(source_id),
            },
        ).fetchone()
        event_id = row.event_id if row else None
        self._batch_cache[cache_key] = event_id
        return event_id
