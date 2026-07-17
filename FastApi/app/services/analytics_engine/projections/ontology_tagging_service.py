from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import text

from ..core.config import EVENT_FACT, EVENT_ONTOLOGY_TAG, SCHEMA_NAME


class OntologyTaggingService:
    """
    Phase 1:
      - derives ontology tags from event_fact + event_meta
      - writes them back to event_fact.event_meta["ontology"]

    Phase 2:
      - also persists one row per tag into event_ontology_tag when the table exists

    This service is intentionally rule-first and deterministic so it can run in
    every batch without requiring model calls.
    """

    _TOKEN_RE = re.compile(r"\W+")
    _NEGATIVE_WORDS = {
        "issue", "problem", "complaint", "angry", "bad", "poor", "delay", "late",
        "refund", "stuck", "broken", "escalate", "escalation", "frustrated", "unhappy",
        "cancel", "cancellation", "exit", "terminate",
    }
    _POSITIVE_WORDS = {
        "thanks", "thank", "great", "good", "awesome", "resolved", "fixed",
        "helpful", "happy", "excellent", "love",
    }

    def __init__(self, db):
        self.db = db
        self._batch_cache: dict[int, dict[str, Any]] = {}
        self._table_exists_cache: dict[str, bool] = {}
        self._sql_fetch_event = text(
            f"""
            SELECT
                event_id,
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
            FROM {EVENT_FACT}
            WHERE event_id = :event_id
            """
        )
        self._sql_update_event_meta = text(
            f"""
            UPDATE {EVENT_FACT}
            SET
                event_meta = jsonb_set(
                    COALESCE(event_meta, '{{}}'::jsonb),
                    '{{ontology}}',
                    CAST(:ontology_json AS jsonb),
                    true
                )
            WHERE event_id = :event_id
            """
        )
        self._sql_upsert_tag = text(
            f"""
            INSERT INTO {EVENT_ONTOLOGY_TAG}
            (
                event_id,
                namespace,
                value,
                is_primary,
                confidence,
                source,
                evidence_json
            )
            VALUES
            (
                :event_id,
                :namespace,
                :value,
                :is_primary,
                :confidence,
                :source,
                CAST(:evidence_json AS JSONB)
            )
            ON CONFLICT (event_id, namespace, value)
            DO UPDATE SET
                is_primary = EXCLUDED.is_primary,
                confidence = EXCLUDED.confidence,
                source = EXCLUDED.source,
                evidence_json = EXCLUDED.evidence_json,
                updated_at = NOW()
            """
        )

    def reset_batch_cache(self):
        self._batch_cache = {}

    def _table_exists(self, table_name: str) -> bool:
        cache_key = table_name
        if cache_key in self._table_exists_cache:
            return self._table_exists_cache[cache_key]

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
        self._table_exists_cache[cache_key] = present
        return present

    def _load_event(self, event_id: int) -> dict[str, Any] | None:
        row = self.db.execute(self._sql_fetch_event, {"event_id": int(event_id)}).mappings().fetchone()
        return dict(row) if row else None

    def _normalize_table_name(self, source_table: str | None) -> str:
        text_value = str(source_table or "").replace('"', "").strip()
        if "." in text_value:
            text_value = text_value.split(".")[-1]
        return text_value.lower()

    def _meta_dict(self, event_meta: Any) -> dict[str, Any]:
        if event_meta is None:
            return {}
        if isinstance(event_meta, dict):
            return dict(event_meta)
        if isinstance(event_meta, str):
            try:
                value = json.loads(event_meta)
                return value if isinstance(value, dict) else {}
            except Exception:
                return {}
        try:
            return dict(event_meta)
        except Exception:
            return {}

    def _stringify(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple, set)):
            return " ".join(self._stringify(item) for item in value)
        return str(value)

    def _text_blob(self, event_row: dict[str, Any], meta: dict[str, Any]) -> str:
        interesting_keys = (
            "subject",
            "snippet",
            "body",
            "description",
            "ticket_feedback",
            "comment",
            "comments",
            "sales_comment",
            "stay_comment",
            "welcome_comment",
            "suggestions",
            "other_comment",
            "audit_history",
            "category",
            "current_page",
            "content_preview",
            "message_type",
            "direction",
            "raw_status",
            "status",
            "payment_mode",
            "transaction_type",
            "origin",
            "transcript_text",
            "translated_text",
            "raw_transcripts",
            "intent",
            "emotion",
            "tone",
            "outcome",
            "context",
            "action_layer",
        )
        parts = [
            event_row.get("event_name"),
            event_row.get("event_family"),
            event_row.get("event_channel"),
            event_row.get("event_status"),
        ]
        for key in interesting_keys:
            if key in meta:
                parts.append(meta.get(key))
        return " ".join(self._stringify(part) for part in parts if part not in (None, "")).lower()

    def _tokens(self, text_blob: str) -> set[str]:
        return {token for token in self._TOKEN_RE.split(text_blob.lower()) if token}

    def _numeric(self, value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except Exception:
            return None

    def _tag_record(self, namespace: str, value: str, confidence: float, reason: str, event_id: int):
        return {
            "event_id": int(event_id),
            "namespace": str(namespace),
            "value": str(value),
            "is_primary": True,
            "confidence": round(float(confidence), 4),
            "source": "rule_engine_v1",
            "evidence": {
                "reason": reason,
            },
        }

    def _append_tag(self, tags: dict[str, dict[str, Any]], namespace: str, value: str | None, confidence: float, reason: str, event_id: int):
        if not value:
            return
        current = tags.get(namespace)
        if current is None or float(confidence) >= float(current["confidence"]):
            tags[namespace] = self._tag_record(namespace, value, confidence, reason, event_id)

    def _keyword_any(self, tokens: set[str], *words: str) -> bool:
        return any(word in tokens for word in words)

    def _derive_issue_category(self, tokens: set[str], meta: dict[str, Any]) -> str | None:
        category = self._stringify(meta.get("category")).lower()
        text = " ".join([category, self._stringify(meta.get("description")).lower(), self._stringify(meta.get("comment")).lower()])

        if any(word in text for word in ("delay", "late", "pending")):
            return "delay"
        if any(word in text for word in ("bill", "billing", "invoice")):
            return "billing"
        if any(word in text for word in ("payment", "utr", "failed", "declined", "bounce")):
            return "payment_failure"
        if any(word in text for word in ("wifi", "internet", "network", "technical", "app", "login", "access", "otp")):
            return "technical_issue" if "technical" in text or "wifi" in text or "internet" in text else "access_or_login"
        if any(word in text for word in ("clean", "dirty", "quality", "hygiene", "service quality")):
            return "quality"
        if any(word in text for word in ("staff", "executive", "behavior", "rude")):
            return "staff_behavior"
        if any(word in text for word in ("facility", "maintenance", "plumbing", "electric", "ac", "water", "room", "building")):
            return "facility_or_operations_issue"
        if any(word in text for word in ("document", "kyc", "verification", "proof")):
            return "data_or_document_issue"
        if any(word in text for word in ("policy", "expectation", "mismatch")):
            return "policy_or_expectation_mismatch"
        if any(word in text for word in ("communication", "reply", "respond", "callback")):
            return "communication_gap"
        if "ticket" in tokens or "issue" in tokens or "complaint" in tokens:
            return "other_issue"
        return None

    def _derive_sentiment(self, tokens: set[str], event_row: dict[str, Any], meta: dict[str, Any]) -> str | None:
        ratings = [
            self._numeric(meta.get("stay_rating")),
            self._numeric(meta.get("sales_rating")),
            self._numeric(meta.get("cleaning_rating")),
            self._numeric(meta.get("rms_rating")),
            self._numeric(meta.get("building_rating")),
            self._numeric(meta.get("refer_friends_score")),
            self._numeric(meta.get("ticket_rating")),
            self._numeric(event_row.get("metric_value")) if str(event_row.get("metric_name") or "").endswith("rating") else None,
        ]
        ratings = [rating for rating in ratings if rating is not None]
        if ratings:
            avg = sum(ratings) / len(ratings)
            if avg >= 4:
                return "positive"
            if avg >= 3:
                return "neutral"
            if avg >= 2:
                return "negative"
            return "very_negative"

        neg = len(tokens & self._NEGATIVE_WORDS)
        pos = len(tokens & self._POSITIVE_WORDS)
        if neg and pos:
            return "mixed"
        if neg >= 2:
            return "very_negative"
        if neg == 1:
            return "negative"
        if pos >= 1:
            return "positive"
        return None

    def _derive_tags(self, event_row: dict[str, Any]) -> dict[str, dict[str, Any]]:
        meta = self._meta_dict(event_row.get("event_meta"))
        text_blob = self._text_blob(event_row, meta)
        tokens = self._tokens(text_blob)
        tags: dict[str, dict[str, Any]] = {}
        event_id = int(event_row["event_id"])

        event_name = str(event_row.get("event_name") or "").lower()
        event_family = str(event_row.get("event_family") or "").lower()
        channel = str(event_row.get("event_channel") or "").lower()
        direction = str(event_row.get("event_direction") or "").lower()
        source_table = self._normalize_table_name(event_row.get("source_table"))
        status = str(event_row.get("event_status") or meta.get("status") or "").lower()

        # channel
        channel_value = {
            "call": "call",
            "email": "email",
            "whatsapp": "whatsapp",
            "ticket": "ticket",
            "web": "web",
            "site_visit": "in_person",
            "checkin": "system",
            "checkout": "system",
            "booking": "system",
            "travel_cart": "web",
            "wishlist": "web",
            "lead": "system",
            "user_account": "system",
        }.get(channel)
        if channel_value is None and "ticket" in event_name:
            channel_value = "ticket"
        if channel_value is None and "whatsapp" in event_name:
            channel_value = "whatsapp"
        if channel_value is None and "email" in event_name:
            channel_value = "email"
        if channel_value is None and "call" in event_name:
            channel_value = "call"
        self._append_tag(tags, "channel", channel_value or "system", 0.99, f"channel={channel or source_table}", event_id)

        # direction
        direction_value = None
        if direction in {"inbound", "outbound", "internal", "system"}:
            direction_value = direction
        elif event_name.endswith("_inbound"):
            direction_value = "inbound"
        elif event_name.endswith("_outbound"):
            direction_value = "outbound"
        elif channel in {"booking", "checkin", "checkout", "user_account"}:
            direction_value = "system"
        elif channel == "ticket":
            direction_value = "inbound"
        self._append_tag(tags, "direction", direction_value, 0.98, f"event_direction={direction}", event_id)

        # intent + topic
        if event_name in {"lead_created", "web_page_view"}:
            self._append_tag(tags, "intent_primary", "seek_information", 0.78, event_name, event_id)
        if event_name in {"wishlist_added", "travel_cart_added", "travel_cart_created"}:
            self._append_tag(tags, "intent_primary", "compare_options", 0.90, event_name, event_id)
        if event_name in {"site_visit_scheduled", "site_visit_done", "site_visit_updated"}:
            self._append_tag(tags, "intent_primary", "schedule_or_visit", 0.93, event_name, event_id)
        if event_name in {"booking_confirm", "travel_cart_checkout_initiated"}:
            self._append_tag(tags, "intent_primary", "confirm_or_commit", 0.95, event_name, event_id)
        if event_name in {"booking_audit_updated"}:
            self._append_tag(tags, "intent_primary", "modify_existing", 0.82, event_name, event_id)
        if event_name.startswith("invoice_"):
            if "utr" in event_name or self._keyword_any(tokens, "payment", "utr", "paid"):
                self._append_tag(tags, "intent_primary", "make_payment", 0.95, event_name, event_id)
            else:
                self._append_tag(tags, "intent_primary", "follow_up", 0.72, event_name, event_id)
        if event_name.startswith("ticket_"):
            self._append_tag(tags, "intent_primary", "request_support" if event_name == "ticket_created" else "follow_up", 0.95, event_name, event_id)
        if event_name in {"email_inbound", "whatsapp_inbound", "call"} and direction_value == "inbound":
            if self._keyword_any(tokens, "support", "issue", "complaint", "problem"):
                self._append_tag(tags, "intent_primary", "complain_or_escalate", 0.70, "inbound_issue_keywords", event_id)
            elif self._keyword_any(tokens, "visit", "schedule", "tour"):
                self._append_tag(tags, "intent_primary", "schedule_or_visit", 0.66, "inbound_schedule_keywords", event_id)
            elif self._keyword_any(tokens, "payment", "invoice", "refund"):
                self._append_tag(tags, "intent_primary", "make_payment", 0.64, "inbound_payment_keywords", event_id)
            else:
                self._append_tag(tags, "intent_primary", "follow_up", 0.55, "generic_inbound_comm", event_id)
        if event_name in {"email_outbound", "whatsapp_outbound"}:
            self._append_tag(tags, "intent_primary", "follow_up", 0.65, "generic_outbound_comm", event_id)
        if event_name in {"checkout_completed"}:
            self._append_tag(tags, "intent_primary", "provide_feedback", 0.72, event_name, event_id)

        if channel in {"web", "site_visit", "travel_cart", "wishlist"} or self._keyword_any(tokens, "property", "unit", "inventory"):
            self._append_tag(tags, "topic_primary", "offering_or_inventory", 0.80, channel or "inventory_signal", event_id)
        if self._keyword_any(tokens, "price", "pricing", "rent", "amount", "commercial", "discount"):
            self._append_tag(tags, "topic_primary", "pricing_or_commercials", 0.84, "pricing_keywords", event_id)
        if self._keyword_any(tokens, "availability", "visit", "schedule", "date", "checkin", "checkout", "timeline"):
            self._append_tag(tags, "topic_primary", "availability_or_timeline", 0.82, "timeline_keywords", event_id)
        if event_name == "booking_confirm":
            self._append_tag(tags, "topic_primary", "commitment_or_contract", 0.97, event_name, event_id)
        if event_name == "checkin_completed":
            self._append_tag(tags, "topic_primary", "onboarding_or_activation", 0.96, event_name, event_id)
        if event_name == "checkout_completed":
            self._append_tag(tags, "topic_primary", "renewal_or_exit", 0.92, event_name, event_id)
        if event_name.startswith("ticket_"):
            self._append_tag(tags, "topic_primary", "support_or_issue", 0.98, event_name, event_id)
        if event_name.startswith("invoice_"):
            if "utr" in event_name or self._keyword_any(tokens, "payment", "refund", "utr"):
                self._append_tag(tags, "topic_primary", "payment_or_refund", 0.92, event_name, event_id)
            else:
                self._append_tag(tags, "topic_primary", "billing_or_invoice", 0.92, event_name, event_id)
        if "kyc" in tokens or "document" in tokens or "verification" in tokens:
            self._append_tag(tags, "topic_primary", "documents_or_verification", 0.86, "document_keywords", event_id)
        if "feedback" in tokens or "rating" in tokens:
            self._append_tag(tags, "topic_primary", "service_experience", 0.76, "feedback_keywords", event_id)

        # issue_category
        issue_category = self._derive_issue_category(tokens, meta)
        if issue_category:
            self._append_tag(tags, "issue_category", issue_category, 0.90 if event_name.startswith("ticket_") else 0.65, "issue_derivation", event_id)

        # journey_stage
        if event_name == "lead_created":
            self._append_tag(tags, "journey_stage", "new_or_unqualified", 0.94, event_name, event_id)
        elif event_name in {"whatsapp_inbound", "whatsapp_outbound", "call", "site_visit_scheduled", "site_visit_done"}:
            self._append_tag(tags, "journey_stage", "engaged_discovery", 0.72, event_name, event_id)
        elif event_name in {"wishlist_added", "travel_cart_added", "travel_cart_created", "web_page_view", "site_visit_updated"}:
            self._append_tag(tags, "journey_stage", "active_evaluation", 0.78, event_name, event_id)
        elif event_name in {"travel_cart_checkout_initiated"}:
            self._append_tag(tags, "journey_stage", "decision_pending", 0.82, event_name, event_id)
        elif event_name == "booking_confirm":
            self._append_tag(tags, "journey_stage", "commitment_in_progress", 0.97, event_name, event_id)
        elif event_name == "checkin_completed":
            self._append_tag(tags, "journey_stage", "onboarding_or_activation", 0.98, event_name, event_id)
        elif event_name.startswith("invoice_"):
            self._append_tag(tags, "journey_stage", "active_service_early", 0.68, event_name, event_id)
        elif event_name.startswith("ticket_"):
            self._append_tag(tags, "journey_stage", "issue_recovery", 0.98, event_name, event_id)
        elif event_name == "checkout_completed":
            self._append_tag(tags, "journey_stage", "exit_or_offboarding", 0.98, event_name, event_id)
        elif event_name == "lead_closed":
            if self._keyword_any(tokens, "lost", "junk", "spam", "closed", "inactive"):
                self._append_tag(tags, "journey_stage", "inactive_or_lost", 0.83, event_name, event_id)
            elif self._keyword_any(tokens, "cancel", "exit", "drop"):
                self._append_tag(tags, "journey_stage", "exit_or_offboarding", 0.76, event_name, event_id)
        elif event_name in {"booking_audit_updated"}:
            self._append_tag(tags, "journey_stage", "active_service_steady", 0.62, event_name, event_id)

        # sentiment
        sentiment = self._derive_sentiment(tokens, event_row, meta)
        self._append_tag(tags, "sentiment", sentiment, 0.88 if sentiment else 0.0, "sentiment_derivation", event_id)

        # urgency
        priority_text = " ".join([
            self._stringify(meta.get("priority")).lower(),
            self._stringify(meta.get("reopen_flag")).lower(),
            text_blob,
        ])
        urgency = None
        if any(word in priority_text for word in ("critical", "severe", "emergency")):
            urgency = "critical"
        elif any(word in priority_text for word in ("urgent", "p1", "high")):
            urgency = "urgent" if "urgent" in priority_text else "high"
        elif any(word in priority_text for word in ("asap", "immediately", "today")):
            urgency = "high"
        elif event_name.startswith("ticket_"):
            urgency = "normal"
        self._append_tag(tags, "urgency", urgency, 0.83 if urgency else 0.0, "urgency_derivation", event_id)

        # resolution_stage
        resolution = None
        if event_name == "ticket_created":
            if any(word in status for word in ("reopen", "re-open")) or str(meta.get("reopen_flag")).lower() in {"1", "true", "yes"}:
                resolution = "reopened"
            elif any(word in status for word in ("assigned", "open", "active")):
                resolution = "in_progress"
            else:
                resolution = "new"
        elif event_name == "ticket_resolved":
            resolution = "closed" if "closed" in status else "resolved"
        elif event_name in {"email_inbound", "whatsapp_inbound", "call"} and direction_value == "inbound":
            resolution = "new"
        elif event_name in {"email_outbound", "whatsapp_outbound"}:
            resolution = "awaiting_customer"
        elif event_name == "booking_confirm":
            resolution = "resolved"
        elif event_name.startswith("invoice_"):
            if "utr" in event_name:
                resolution = "resolved"
            else:
                resolution = "awaiting_customer"
        self._append_tag(tags, "resolution_stage", resolution, 0.80 if resolution else 0.0, "resolution_derivation", event_id)

        # ownership_team
        ownership = None
        if event_name.startswith("ticket_"):
            ownership = "support"
            if issue_category == "facility_or_operations_issue":
                ownership = "operations"
        elif event_name.startswith("invoice_"):
            ownership = "finance"
        elif event_name in {"checkin_completed", "checkout_completed"}:
            ownership = "operations"
        elif event_name in {"lead_created", "wishlist_added", "travel_cart_added", "travel_cart_created", "travel_cart_checkout_initiated", "site_visit_scheduled", "site_visit_done", "site_visit_updated", "booking_confirm"}:
            ownership = "sales"
        elif channel in {"email", "whatsapp", "call"}:
            if tags.get("topic_primary", {}).get("value") in {"billing_or_invoice", "payment_or_refund"}:
                ownership = "finance"
            elif tags.get("topic_primary", {}).get("value") == "support_or_issue":
                ownership = "support"
            else:
                ownership = "sales"
        elif event_name == "booking_audit_updated":
            ownership = "operations"
        self._append_tag(tags, "ownership_team", ownership, 0.78 if ownership else 0.0, "ownership_derivation", event_id)

        # relationship_health
        relationship = None
        if tags.get("journey_stage", {}).get("value") == "issue_recovery":
            if tags.get("urgency", {}).get("value") in {"urgent", "critical"} or tags.get("sentiment", {}).get("value") in {"very_negative"}:
                relationship = "critical"
            else:
                relationship = "at_risk"
        elif event_name == "ticket_resolved":
            relationship = "recovering"
        elif event_name in {"checkin_completed", "checkout_completed"} and tags.get("sentiment", {}).get("value") == "positive":
            relationship = "healthy"
        elif event_name == "booking_confirm":
            relationship = "stable"
        elif event_name in {"lead_created", "web_page_view", "wishlist_added", "travel_cart_added", "travel_cart_created"}:
            relationship = "stable"
        elif tags.get("sentiment", {}).get("value") == "negative":
            relationship = "watchlist"
        self._append_tag(tags, "relationship_health", relationship, 0.72 if relationship else 0.0, "relationship_derivation", event_id)

        # next_best_action
        next_action = None
        if event_name == "ticket_created":
            next_action = "resolve_service_issue"
        elif event_name == "ticket_resolved":
            next_action = "close_loop"
        elif event_name == "booking_confirm":
            next_action = "start_onboarding"
        elif event_name == "travel_cart_checkout_initiated":
            next_action = "push_commitment_forward"
        elif event_name in {"wishlist_added", "travel_cart_added", "travel_cart_created", "web_page_view"}:
            next_action = "send_options_or_offer"
        elif event_name in {"site_visit_scheduled", "site_visit_done", "site_visit_updated"}:
            next_action = "schedule_callback_or_visit"
        elif event_name.startswith("invoice_"):
            next_action = "close_loop" if "utr" in event_name else "send_invoice_or_payment_link"
        elif event_name in {"checkin_completed"}:
            next_action = "close_loop"
        elif event_name in {"checkout_completed"}:
            next_action = "retain_or_recover" if tags.get("sentiment", {}).get("value") in {"negative", "very_negative"} else "close_loop"
        elif event_name in {"email_inbound", "whatsapp_inbound", "call"} and direction_value == "inbound":
            next_action = "reply_with_information"
        elif event_name in {"email_outbound", "whatsapp_outbound"}:
            next_action = "awaiting_customer"
        if next_action == "awaiting_customer":
            next_action = "ask_clarifying_question"
        self._append_tag(tags, "next_best_action", next_action, 0.80 if next_action else 0.0, "next_action_derivation", event_id)

        # fallback topic
        if "topic_primary" not in tags:
            self._append_tag(tags, "topic_primary", "general", 0.30, "fallback_topic", event_id)

        return tags

    def _persist_tags_to_event_meta(self, event_id: int, event_row: dict[str, Any], tags: dict[str, dict[str, Any]]) -> None:
        meta = self._meta_dict(event_row.get("event_meta"))
        ontology_meta = {
            "version": "v1",
            "tagged_from": "rule_engine_v1",
            "tags": {namespace: payload["value"] for namespace, payload in tags.items()},
            "confidence": {namespace: payload["confidence"] for namespace, payload in tags.items()},
        }
        meta["ontology"] = ontology_meta
        self.db.execute(
            self._sql_update_event_meta,
            {
                "event_id": int(event_id),
                "ontology_json": json.dumps(ontology_meta, default=str),
            },
        )

    def _persist_tags_to_table(self, tags: dict[str, dict[str, Any]]) -> int:
        if not self._table_exists("event_ontology_tag"):
            return 0

        written = 0
        for payload in tags.values():
            self.db.execute(
                self._sql_upsert_tag,
                {
                    "event_id": payload["event_id"],
                    "namespace": payload["namespace"],
                    "value": payload["value"],
                    "is_primary": payload["is_primary"],
                    "confidence": payload["confidence"],
                    "source": payload["source"],
                    "evidence_json": json.dumps(payload["evidence"], default=str),
                },
            )
            written += 1
        return written

    def tag_event(self, event_id: int, source_table: str | None = None) -> dict[str, Any]:
        event_id = int(event_id)
        cached = self._batch_cache.get(event_id)
        if cached is not None:
            return cached

        event_row = self._load_event(event_id)
        if not event_row:
            result = {"tagged": False, "tags_written": 0}
            self._batch_cache[event_id] = result
            return result

        tags = self._derive_tags(event_row)
        self._persist_tags_to_event_meta(event_id, event_row, tags)
        tags_written = self._persist_tags_to_table(tags)

        result = {
            "tagged": True,
            "tags_written": int(tags_written or len(tags)),
            "tags": {namespace: payload["value"] for namespace, payload in tags.items()},
        }
        self._batch_cache[event_id] = result
        return result

    def get_event_tags(self, event_id: int) -> dict[str, str]:
        event_row = self._load_event(int(event_id))
        if not event_row:
            return {}
        meta = self._meta_dict(event_row.get("event_meta"))
        ontology_meta = self._meta_dict(meta.get("ontology"))
        tags = ontology_meta.get("tags")
        return tags if isinstance(tags, dict) else {}
