from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text

try:
    from app.services.analytics_engine.core.config import SCHEMA_NAME  # type: ignore
except Exception:
    SCHEMA_NAME = "AnalyticsEngine"


@dataclass
class SemanticInterpretation:
    matched: bool
    source: str
    tenant_id: str
    semantic_domain: str
    source_type: str
    raw_value: str | None
    raw_value_normalized: str
    normalized_key: str | None
    normalized_payload: dict[str, Any]
    mapping_key: str | None
    version: int | None
    conditions_json: dict[str, Any]
    priority: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "source": self.source,
            "tenant_id": self.tenant_id,
            "semantic_domain": self.semantic_domain,
            "source_type": self.source_type,
            "raw_value": self.raw_value,
            "raw_value_normalized": self.raw_value_normalized,
            "normalized_key": self.normalized_key,
            "normalized_payload": self.normalized_payload,
            "mapping_key": self.mapping_key,
            "version": self.version,
            "conditions_json": self.conditions_json,
            "priority": self.priority,
        }


class SemanticMappingService:
    """
    Generic tenant semantic registry.

    Supports:
    - DB-backed runtime mappings
    - optional JSON seed fallback
    - conditional mappings using row context
    - generic projection persistence
    """

    def __init__(
        self,
        db,
        *,
        schema: str = SCHEMA_NAME,
        fallback_seed_files: list[str] | None = None,
    ) -> None:
        self.db = db
        self.schema = schema
        self.fallback_seed_files = [str(p) for p in (fallback_seed_files or [])]
        self._seed_cache: dict[str, list[dict[str, Any]]] = {}

    @staticmethod
    def normalize_raw_value(value: Any) -> str:
        if value is None:
            return ""
        return " ".join(str(value).strip().lower().split())

    @staticmethod
    def _normalize_scalar(value: Any) -> Any:
        if isinstance(value, str):
            return " ".join(value.strip().lower().split())
        return value

    def _load_seed_rows(self, tenant_id: str) -> list[dict[str, Any]]:
        cache_key = tenant_id
        if cache_key in self._seed_cache:
            return self._seed_cache[cache_key]

        rows: list[dict[str, Any]] = []
        for seed_file in self.fallback_seed_files:
            path = Path(seed_file)
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            for row in data:
                if str(row.get("tenant_id") or "").strip() != tenant_id:
                    continue
                rows.append(dict(row))

        self._seed_cache[cache_key] = rows
        return rows

    def _fetch_db_candidates(
        self,
        *,
        tenant_id: str,
        semantic_domain: str,
        source_type: str,
        raw_value_normalized: str,
    ) -> list[dict[str, Any]]:
        rows = self.db.execute(
            text(
                f'''
                SELECT
                    mapping_key,
                    tenant_id,
                    semantic_domain,
                    source_type,
                    raw_value,
                    raw_value_normalized,
                    normalized_key,
                    normalized_payload_json,
                    conditions_json,
                    priority,
                    version
                FROM "{self.schema}".tenant_semantic_mapping
                WHERE tenant_id = :tenant_id
                  AND semantic_domain = :semantic_domain
                  AND source_type = :source_type
                  AND is_active = TRUE
                  AND (
                        raw_value_normalized = :raw_value_normalized
                        OR raw_value_normalized = '*'
                  )
                ORDER BY
                    CASE WHEN raw_value_normalized = :raw_value_normalized THEN 0 ELSE 1 END,
                    priority ASC,
                    version DESC,
                    mapping_key ASC
                '''
            ),
            {
                "tenant_id": tenant_id,
                "semantic_domain": semantic_domain,
                "source_type": source_type,
                "raw_value_normalized": raw_value_normalized,
            },
        ).mappings().fetchall()
        return [dict(row) for row in rows]

    def _fetch_seed_candidates(
        self,
        *,
        tenant_id: str,
        semantic_domain: str,
        source_type: str,
        raw_value_normalized: str,
    ) -> list[dict[str, Any]]:
        rows = []
        for row in self._load_seed_rows(tenant_id):
            if str(row.get("semantic_domain")) != semantic_domain:
                continue
            if str(row.get("source_type")) != source_type:
                continue
            raw_norm = str(row.get("raw_value_normalized") or self.normalize_raw_value(row.get("raw_value")))
            if raw_norm not in (raw_value_normalized, "*"):
                continue
            copied = dict(row)
            copied["raw_value_normalized"] = raw_norm
            rows.append(copied)

        rows.sort(
            key=lambda row: (
                0 if row.get("raw_value_normalized") == raw_value_normalized else 1,
                int(row.get("priority", 100)),
                -int(row.get("version", 1)),
                str(row.get("mapping_key", "")),
            )
        )
        return rows

    def _coerce_number(self, value: Any) -> float | None:
        if value in (None, "", "null", "NULL", "NA", "N/A"):
            return None
        try:
            return float(value)
        except Exception:
            return None

    def _match_condition(self, condition: dict[str, Any], row_context: dict[str, Any]) -> bool:
        field = condition.get("field")
        op = str(condition.get("op") or "eq").strip().lower()
        expected = condition.get("value")
        actual = row_context.get(field) if field is not None else None

        if op == "exists":
            return actual is not None
        if op == "not_exists":
            return actual is None

        actual_num = self._coerce_number(actual)
        expected_num = self._coerce_number(expected)

        if op == "eq":
            return self._normalize_scalar(actual) == self._normalize_scalar(expected)
        if op == "ne":
            return self._normalize_scalar(actual) != self._normalize_scalar(expected)
        if op == "in":
            values = expected if isinstance(expected, list) else [expected]
            normalized_values = {self._normalize_scalar(v) for v in values}
            return self._normalize_scalar(actual) in normalized_values
        if op == "not_in":
            values = expected if isinstance(expected, list) else [expected]
            normalized_values = {self._normalize_scalar(v) for v in values}
            return self._normalize_scalar(actual) not in normalized_values
        if op == "contains":
            return str(expected or "") in str(actual or "")
        if op == "icontains":
            return self.normalize_raw_value(expected) in self.normalize_raw_value(actual)
        if op in {"lt", "lte", "gt", "gte"}:
            if actual_num is None or expected_num is None:
                return False
            if op == "lt":
                return actual_num < expected_num
            if op == "lte":
                return actual_num <= expected_num
            if op == "gt":
                return actual_num > expected_num
            if op == "gte":
                return actual_num >= expected_num
        return False

    def _matches_conditions(self, conditions: Any, row_context: dict[str, Any]) -> bool:
        if not conditions:
            return True

        if isinstance(conditions, list):
            return all(self._matches_conditions(item, row_context) for item in conditions)

        if not isinstance(conditions, dict):
            return False

        if "all" in conditions:
            items = conditions.get("all") or []
            return all(self._matches_conditions(item, row_context) for item in items)

        if "any" in conditions:
            items = conditions.get("any") or []
            return any(self._matches_conditions(item, row_context) for item in items)

        if "not" in conditions:
            return not self._matches_conditions(conditions.get("not"), row_context)

        if "field" in conditions:
            return self._match_condition(conditions, row_context)

        return not conditions

    def interpret(
        self,
        *,
        tenant_id: str,
        semantic_domain: str,
        source_type: str,
        raw_value: Any,
        row_context: dict[str, Any] | None = None,
    ) -> SemanticInterpretation:
        row_context = row_context or {}
        raw_value_normalized = self.normalize_raw_value(raw_value)

        for source_name, candidate_loader in (
            ("db", self._fetch_db_candidates),
            ("seed", self._fetch_seed_candidates),
        ):
            candidates = candidate_loader(
                tenant_id=tenant_id,
                semantic_domain=semantic_domain,
                source_type=source_type,
                raw_value_normalized=raw_value_normalized,
            )
            for row in candidates:
                conditions = row.get("conditions_json") or {}
                if not self._matches_conditions(conditions, row_context):
                    continue

                payload = row.get("normalized_payload_json") or {}
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {}

                return SemanticInterpretation(
                    matched=True,
                    source=source_name,
                    tenant_id=tenant_id,
                    semantic_domain=semantic_domain,
                    source_type=source_type,
                    raw_value=None if raw_value is None else str(raw_value),
                    raw_value_normalized=raw_value_normalized,
                    normalized_key=row.get("normalized_key"),
                    normalized_payload=payload if isinstance(payload, dict) else {},
                    mapping_key=row.get("mapping_key"),
                    version=int(row.get("version")) if row.get("version") is not None else None,
                    conditions_json=conditions if isinstance(conditions, dict) else {},
                    priority=int(row.get("priority")) if row.get("priority") is not None else None,
                )

        return SemanticInterpretation(
            matched=False,
            source="default",
            tenant_id=tenant_id,
            semantic_domain=semantic_domain,
            source_type=source_type,
            raw_value=None if raw_value is None else str(raw_value),
            raw_value_normalized=raw_value_normalized,
            normalized_key=None,
            normalized_payload={},
            mapping_key=None,
            version=None,
            conditions_json={},
            priority=None,
        )

    def upsert_mapping(
        self,
        *,
        tenant_id: str,
        mapping_key: str,
        semantic_domain: str,
        source_type: str,
        raw_value: str,
        normalized_key: str,
        normalized_payload_json: dict[str, Any] | None = None,
        conditions_json: dict[str, Any] | None = None,
        priority: int = 100,
        is_active: bool = True,
        version: int = 1,
        notes: str | None = None,
        updated_by: str | None = None,
    ) -> None:
        payload_json = json.dumps(normalized_payload_json or {}, ensure_ascii=False)
        conditions_json_text = json.dumps(conditions_json or {}, ensure_ascii=False)
        raw_value_normalized = self.normalize_raw_value(raw_value)

        self.db.execute(
            text(
                f'''
                INSERT INTO "{self.schema}".tenant_semantic_mapping
                (
                    mapping_key,
                    tenant_id,
                    semantic_domain,
                    source_type,
                    raw_value,
                    raw_value_normalized,
                    normalized_key,
                    normalized_payload_json,
                    conditions_json,
                    priority,
                    is_active,
                    version,
                    notes,
                    created_by,
                    updated_by
                )
                VALUES
                (
                    :mapping_key,
                    :tenant_id,
                    :semantic_domain,
                    :source_type,
                    :raw_value,
                    :raw_value_normalized,
                    :normalized_key,
                    CAST(:normalized_payload_json AS JSONB),
                    CAST(:conditions_json AS JSONB),
                    :priority,
                    :is_active,
                    :version,
                    :notes,
                    :updated_by,
                    :updated_by
                )
                ON CONFLICT (tenant_id, mapping_key)
                DO UPDATE SET
                    semantic_domain = EXCLUDED.semantic_domain,
                    source_type = EXCLUDED.source_type,
                    raw_value = EXCLUDED.raw_value,
                    raw_value_normalized = EXCLUDED.raw_value_normalized,
                    normalized_key = EXCLUDED.normalized_key,
                    normalized_payload_json = EXCLUDED.normalized_payload_json,
                    conditions_json = EXCLUDED.conditions_json,
                    priority = EXCLUDED.priority,
                    is_active = EXCLUDED.is_active,
                    version = EXCLUDED.version,
                    notes = EXCLUDED.notes,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = NOW()
                '''
            ),
            {
                "mapping_key": mapping_key,
                "tenant_id": tenant_id,
                "semantic_domain": semantic_domain,
                "source_type": source_type,
                "raw_value": raw_value,
                "raw_value_normalized": raw_value_normalized,
                "normalized_key": normalized_key,
                "normalized_payload_json": payload_json,
                "conditions_json": conditions_json_text,
                "priority": int(priority),
                "is_active": bool(is_active),
                "version": int(version),
                "notes": notes,
                "updated_by": updated_by,
            },
        )

    def upsert_projection(
        self,
        *,
        tenant_id: str,
        source_table: str,
        source_id: str | int,
        semantic_domain: str,
        source_type: str,
        raw_value: Any,
        interpretation: SemanticInterpretation,
        row_context: dict[str, Any] | None = None,
    ) -> None:
        payload_json = json.dumps(interpretation.normalized_payload or {}, ensure_ascii=False)
        row_context_json = json.dumps(row_context or {}, ensure_ascii=False)
        raw_value_normalized = self.normalize_raw_value(raw_value)

        self.db.execute(
            text(
                f'''
                INSERT INTO "{self.schema}".source_semantic_projection
                (
                    tenant_id,
                    source_table,
                    source_id,
                    semantic_domain,
                    source_type,
                    raw_value,
                    raw_value_normalized,
                    normalized_key,
                    normalized_payload_json,
                    matched_mapping_key,
                    matched_version,
                    row_context_json,
                    derived_at,
                    updated_at
                )
                VALUES
                (
                    :tenant_id,
                    :source_table,
                    :source_id,
                    :semantic_domain,
                    :source_type,
                    :raw_value,
                    :raw_value_normalized,
                    :normalized_key,
                    CAST(:normalized_payload_json AS JSONB),
                    :matched_mapping_key,
                    :matched_version,
                    CAST(:row_context_json AS JSONB),
                    NOW(),
                    NOW()
                )
                ON CONFLICT (tenant_id, source_table, source_id, semantic_domain, source_type)
                DO UPDATE SET
                    raw_value = EXCLUDED.raw_value,
                    raw_value_normalized = EXCLUDED.raw_value_normalized,
                    normalized_key = EXCLUDED.normalized_key,
                    normalized_payload_json = EXCLUDED.normalized_payload_json,
                    matched_mapping_key = EXCLUDED.matched_mapping_key,
                    matched_version = EXCLUDED.matched_version,
                    row_context_json = EXCLUDED.row_context_json,
                    derived_at = NOW(),
                    updated_at = NOW()
                '''
            ),
            {
                "tenant_id": tenant_id,
                "source_table": source_table,
                "source_id": str(source_id),
                "semantic_domain": semantic_domain,
                "source_type": source_type,
                "raw_value": None if raw_value is None else str(raw_value),
                "raw_value_normalized": raw_value_normalized,
                "normalized_key": interpretation.normalized_key,
                "normalized_payload_json": payload_json,
                "matched_mapping_key": interpretation.mapping_key,
                "matched_version": interpretation.version,
                "row_context_json": row_context_json,
            },
        )

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
