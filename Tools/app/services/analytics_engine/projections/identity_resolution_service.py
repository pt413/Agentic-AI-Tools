from sqlalchemy import text

from ..core.config import (
    IDENTITY_PERSON,
    IDENTITY_PERSON_KEY,
    IDENTITY_PERSON_MERGE,
    EVENT_PARTICIPANT,
)


class IdentityResolutionService:
    def __init__(self, db):
        self.db = db
        self._cache = {}
        self._person_touched = set()
        self._sql_select_key_owner = text(
            f"""
            SELECT person_id
            FROM {IDENTITY_PERSON_KEY}
            WHERE key_type = :kt
              AND key_value = :kv
            LIMIT 1
            """
        )
        self._sql_find_people_template = """
            SELECT DISTINCT person_id
            FROM {identity_person_key}
            WHERE {conditions}
            ORDER BY person_id
        """
        self._sql_create_person = text(
            f"""
            INSERT INTO {IDENTITY_PERSON}
            (
                canonical_name,
                primary_phone,
                primary_email,
                person_kind,
                kind_confidence,
                first_seen_at,
                last_seen_at
            )
            VALUES
            (
                :canonical_name,
                :primary_phone,
                :primary_email,
                COALESCE(:person_kind, 'unknown'),
                :kind_confidence,
                :t,
                :t
            )
            RETURNING person_id
            """
        )
        self._sql_touch_person = text(
            f"""
            UPDATE {IDENTITY_PERSON}
            SET
                primary_phone = COALESCE(primary_phone, :primary_phone),
                primary_email = COALESCE(primary_email, :primary_email),
                canonical_name = COALESCE(canonical_name, :canonical_name),
                person_kind = CASE
                    WHEN :person_kind IS NULL THEN person_kind
                    WHEN person_kind IS NULL OR person_kind = 'unknown' THEN :person_kind
                    ELSE person_kind
                END,
                kind_confidence = CASE
                    WHEN :person_kind IS NULL THEN kind_confidence
                    WHEN person_kind IS NULL OR person_kind = 'unknown' THEN GREATEST(COALESCE(kind_confidence, 0), :kind_confidence)
                    ELSE kind_confidence
                END,
                first_seen_at = CASE
                    WHEN :t IS NULL THEN first_seen_at
                    WHEN first_seen_at IS NULL THEN :t
                    WHEN :t < first_seen_at THEN :t
                    ELSE first_seen_at
                END,
                last_seen_at = CASE
                    WHEN :t IS NULL THEN last_seen_at
                    WHEN last_seen_at IS NULL THEN :t
                    WHEN :t > last_seen_at THEN :t
                    ELSE last_seen_at
                END,
                updated_at = NOW()
            WHERE person_id = :person_id
            """
        )
        self._sql_attach_key = text(
            f"""
            INSERT INTO {IDENTITY_PERSON_KEY}
            (
                person_id,
                key_type,
                key_value,
                source_table,
                source_id,
                first_seen_at,
                last_seen_at
            )
            VALUES
            (
                :p,
                :kt,
                :kv,
                :st,
                :sid,
                :t,
                :t
            )
            ON CONFLICT (key_type, key_value)
            DO UPDATE SET
                updated_at = NOW(),
                first_seen_at = CASE
                    WHEN EXCLUDED.first_seen_at IS NULL THEN {IDENTITY_PERSON_KEY}.first_seen_at
                    WHEN {IDENTITY_PERSON_KEY}.first_seen_at IS NULL THEN EXCLUDED.first_seen_at
                    WHEN EXCLUDED.first_seen_at < {IDENTITY_PERSON_KEY}.first_seen_at THEN EXCLUDED.first_seen_at
                    ELSE {IDENTITY_PERSON_KEY}.first_seen_at
                END,
                last_seen_at = CASE
                    WHEN EXCLUDED.last_seen_at IS NULL THEN {IDENTITY_PERSON_KEY}.last_seen_at
                    WHEN {IDENTITY_PERSON_KEY}.last_seen_at IS NULL THEN EXCLUDED.last_seen_at
                    WHEN EXCLUDED.last_seen_at > {IDENTITY_PERSON_KEY}.last_seen_at THEN EXCLUDED.last_seen_at
                    ELSE {IDENTITY_PERSON_KEY}.last_seen_at
                END
            RETURNING person_id
            """
        )

    def reset_batch_cache(self):
        self._cache = {}
        self._person_touched = set()

    def _cache_key(self, key_type: str, key_value: str):
        return (str(key_type).strip().lower(), str(key_value).strip())

    def _normalize_key(self, key_type: str, key_value: str):
        if key_value is None:
            return None

        kt = str(key_type).strip().lower()
        kv = str(key_value).strip()
        if not kt or not kv:
            return None

        if kt == "staff_ref":
            kt = "username"

        if kt in ("email", "username"):
            kv = kv.lower()

        return kt, kv

    def _identity_seed_fields(self, key_type: str, key_value: str):
        primary_phone = None
        primary_email = None
        canonical_name = None

        if key_type == "phone":
            primary_phone = key_value
        elif key_type == "email":
            primary_email = key_value
        elif key_type in ("staff_ref", "username", "name"):
            canonical_name = key_value

        return {
            "primary_phone": primary_phone,
            "primary_email": primary_email,
            "canonical_name": canonical_name,
        }

    def _merge_seed_fields(self, candidate_keys, seed_fields=None):
        result = {
            "primary_phone": None,
            "primary_email": None,
            "canonical_name": None,
            "person_kind": None,
            "kind_confidence": 0,
        }

        for key_type, key_value in candidate_keys:
            seed = self._identity_seed_fields(key_type, key_value)
            result["primary_phone"] = result["primary_phone"] or seed["primary_phone"]
            result["primary_email"] = result["primary_email"] or seed["primary_email"]
            result["canonical_name"] = result["canonical_name"] or seed["canonical_name"]

        if seed_fields:
            result["canonical_name"] = result["canonical_name"] or seed_fields.get("canonical_name")
            result["primary_phone"] = result["primary_phone"] or seed_fields.get("primary_phone")
            result["primary_email"] = result["primary_email"] or seed_fields.get("primary_email")
            result["person_kind"] = result["person_kind"] or seed_fields.get("person_kind")
            result["kind_confidence"] = max(
                result.get("kind_confidence", 0) or 0,
                seed_fields.get("kind_confidence", 0) or 0,
            )

        return result

    def _touch_person(self, person_id, seed_fields, event_time=None):
        if not person_id:
            return
        touch_key = (int(person_id), event_time)
        if touch_key in self._person_touched:
            return
        self._person_touched.add(touch_key)
        self.db.execute(
            self._sql_touch_person,
            {
                "person_id": person_id,
                "primary_phone": seed_fields.get("primary_phone"),
                "primary_email": seed_fields.get("primary_email"),
                "canonical_name": seed_fields.get("canonical_name"),
                "person_kind": seed_fields.get("person_kind"),
                "kind_confidence": seed_fields.get("kind_confidence", 0) or 0,
                "t": event_time,
            },
        )

    def _create_person(self, seed_fields, event_time=None):
        row = self.db.execute(
            self._sql_create_person,
            {
                "canonical_name": seed_fields.get("canonical_name"),
                "primary_phone": seed_fields.get("primary_phone"),
                "primary_email": seed_fields.get("primary_email"),
                "person_kind": seed_fields.get("person_kind"),
                "kind_confidence": seed_fields.get("kind_confidence", 0) or 0,
                "t": event_time,
            },
        ).fetchone()
        return row.person_id

    def _find_existing_key_owner(self, key_type, key_value):
        cached = self._cache.get(self._cache_key(key_type, key_value))
        if cached:
            class RowObj:
                def __init__(self, person_id):
                    self.person_id = person_id
            return RowObj(cached)
        row = self.db.execute(self._sql_select_key_owner, {"kt": key_type, "kv": key_value}).fetchone()
        if row:
            self._cache[self._cache_key(key_type, key_value)] = row.person_id
        return row

    def find_person_ids_by_keys(self, candidate_keys):
        normalized = []
        cached_ids = set()
        for key_type, key_value in candidate_keys:
            nk = self._normalize_key(key_type, key_value)
            if nk:
                normalized.append(nk)
                cached = self._cache.get(self._cache_key(*nk))
                if cached:
                    cached_ids.add(cached)

        if not normalized:
            return []

        if len(cached_ids) == 1:
            return list(cached_ids)

        conditions = []
        params = {}
        for i, (key_type, key_value) in enumerate(normalized):
            conditions.append(f"(key_type = :kt{i} AND key_value = :kv{i})")
            params[f"kt{i}"] = key_type
            params[f"kv{i}"] = key_value

        rows = self.db.execute(
            text(
                self._sql_find_people_template.format(
                    identity_person_key=IDENTITY_PERSON_KEY,
                    conditions=' OR '.join(conditions),
                )
            ),
            params,
        ).fetchall()
        person_ids = [row.person_id for row in rows]
        if len(person_ids) == 1:
            for key_type, key_value in normalized:
                self._cache[self._cache_key(key_type, key_value)] = person_ids[0]
        return person_ids

    def choose_canonical_person(self, person_ids, preferred_person_id=None):
        unique_ids = []
        seen = set()
        for person_id in person_ids:
            if person_id and person_id not in seen:
                seen.add(person_id)
                unique_ids.append(person_id)

        if not unique_ids:
            return None
        if preferred_person_id in seen:
            return preferred_person_id
        if len(unique_ids) == 1:
            return unique_ids[0]

        rows = self.db.execute(
            text(
                f"""
                SELECT person_id,
                       CASE WHEN primary_email IS NOT NULL THEN 1 ELSE 0 END AS has_email,
                       CASE WHEN canonical_name IS NOT NULL THEN 1 ELSE 0 END AS has_name,
                       CASE WHEN primary_phone IS NOT NULL THEN 1 ELSE 0 END AS has_phone,
                       first_seen_at,
                       created_at
                FROM {IDENTITY_PERSON}
                WHERE person_id = ANY(:person_ids)
                AND merged_into_person_id IS NULL
                ORDER BY has_email DESC,
                         has_name DESC,
                         has_phone DESC,
                         first_seen_at NULLS LAST,
                         created_at NULLS LAST,
                         person_id ASC
                LIMIT 1
                """
            ),
            {"person_ids": unique_ids},
        ).fetchone()
        return rows.person_id if rows else unique_ids[0]

    def merge_people(
        self,
        canonical_person_id,
        merged_person_id,
        merge_reason="identity_resolution",
        merge_source_table=None,
        merge_source_id=None,
        event_time=None,
        notes=None,
    ):
        if (
            not canonical_person_id
            or not merged_person_id
            or canonical_person_id == merged_person_id
        ):
            return canonical_person_id
        canonical = self.db.execute(
            text(
                f"""
                SELECT
                    person_id,
                    canonical_name,
                    primary_phone,
                    primary_email,
                    person_kind,
                    kind_confidence,
                    first_seen_at,
                    last_seen_at
                FROM {IDENTITY_PERSON}
                WHERE person_id = :pid
                """
            ),
            {"pid": canonical_person_id},
        ).mappings().fetchone()

        merged = self.db.execute(
            text(
                f"""
                SELECT
                    person_id,
                    canonical_name,
                    primary_phone,
                    primary_email,
                    person_kind,
                    kind_confidence,
                    first_seen_at,
                    last_seen_at
                FROM {IDENTITY_PERSON}
                WHERE person_id = :pid
                """
            ),
            {"pid": merged_person_id},
        ).mappings().fetchone()
        if not canonical or not merged:
            return canonical_person_id

        self.db.execute(
            text(
                f"""
                INSERT INTO {IDENTITY_PERSON_MERGE}
                (canonical_person_id, merged_person_id, merge_reason, merge_source_table, merge_source_id, notes)
                VALUES (:canonical_person_id, :merged_person_id, :merge_reason, :merge_source_table, :merge_source_id, :notes)
                ON CONFLICT (canonical_person_id, merged_person_id) DO NOTHING
                """
            ),
            {
                "canonical_person_id": canonical_person_id,
                "merged_person_id": merged_person_id,
                "merge_reason": merge_reason,
                "merge_source_table": merge_source_table,
                "merge_source_id": str(merge_source_id) if merge_source_id is not None else None,
                "notes": notes,
            },
        )

        self.db.execute(
            text(
                f"""
                UPDATE {EVENT_PARTICIPANT}
                SET person_id = :canonical_person_id,
                    resolution_method = COALESCE(resolution_method, 'person_merge'),
                    resolved_at = COALESCE(resolved_at, NOW())
                WHERE person_id = :merged_person_id
                """
            ),
            {"canonical_person_id": canonical_person_id, "merged_person_id": merged_person_id},
        )

        self.db.execute(
            text(
                f"""
                UPDATE {IDENTITY_PERSON_KEY}
                SET person_id = :canonical_person_id,
                    updated_at = NOW()
                WHERE person_id = :merged_person_id
                """
            ),
            {
                "canonical_person_id": canonical_person_id,
                "merged_person_id": merged_person_id,
            },
        )

        seed_fields = {
            "canonical_name": canonical["canonical_name"] or merged["canonical_name"],
            "primary_phone": canonical["primary_phone"] or merged["primary_phone"],
            "primary_email": canonical["primary_email"] or merged["primary_email"],
            "person_kind": canonical["person_kind"] or merged["person_kind"],
            "kind_confidence": max(
                canonical["kind_confidence"] or 0,
                merged["kind_confidence"] or 0,
            ),
        }
        self._touch_person(canonical_person_id, seed_fields, event_time=event_time)

        self.db.execute(
            text(
                f"""
                UPDATE {IDENTITY_PERSON}
                SET merged_into_person_id = :canonical_person_id,
                    last_seen_at = COALESCE(last_seen_at, :event_time),
                    updated_at = NOW()
                WHERE person_id = :merged_person_id
                """
            ),
            {"canonical_person_id": canonical_person_id, "merged_person_id": merged_person_id, "event_time": event_time},
        )

        cache_updates = self.db.execute(
            text(
                f"""
                SELECT key_type, key_value
                FROM {IDENTITY_PERSON_KEY}
                WHERE person_id = :canonical_person_id
                """
            ),
            {"canonical_person_id": canonical_person_id},
        ).fetchall()
        for row in cache_updates:
            self._cache[self._cache_key(row.key_type, row.key_value)] = canonical_person_id

        return canonical_person_id

    def attach_key_to_person(
        self,
        person_id,
        key_type,
        key_value,
        source_table,
        source_id,
        event_time=None,
        follow_existing=True,
    ):
        nk = self._normalize_key(key_type, key_value)
        if not nk:
            return None

        key_type, key_value = nk
        row = self.db.execute(
            self._sql_attach_key,
            {
                "p": person_id,
                "kt": key_type,
                "kv": key_value,
                "st": source_table,
                "sid": str(source_id),
                "t": event_time,
            },
        ).fetchone()

        existing_person_id = row.person_id if row else None
        resolved_person_id = existing_person_id or person_id
        self._cache[self._cache_key(key_type, key_value)] = resolved_person_id
        seed = self._identity_seed_fields(key_type, key_value)
        self._touch_person(resolved_person_id, seed, event_time=event_time)

        if follow_existing:
            return resolved_person_id
        return person_id

    def _is_strong_key(self, key_type: str) -> bool:
        """
        Phone and email are globally unique person identifiers.
        booking_id and lead_id are also reliable: one booking/lead = one person.
        user_id is intentionally excluded because the same table holds
        employees, customers, and tenants — blind merge across entity types
        would corrupt identity.
        """
        return key_type in {"phone", "email", "booking_id", "lead_id"}

    def _should_merge(self, normalized_keys, matched_person_ids) -> bool:
        """
        Merge is safe ONLY when at least two distinct strong keys independently
        agree on the same set of person_ids. A single strong key match is enough
        to anchor, but we need confidence the collision is real, not coincidental.

        Rules:
        - 0 or 1 matched person  → nothing to merge (create or reuse)
        - 2+ matched persons     → merge only if at least one strong key
                                   appears in the candidate set, proving the
                                   link is authoritative, not just a weak-key
                                   collision.
        """
        unique_ids = list(set(matched_person_ids))

        if len(unique_ids) <= 1:
            return False

        strong_in_candidates = any(
            self._is_strong_key(kt) for kt, _ in normalized_keys
        )
        return strong_in_candidates

    def resolve_or_create_person_from_keys(
        self,
        candidate_keys,
        source_table,
        source_id,
        event_time=None,
        seed_fields=None,
        merge_reason="bridged_by_source_row",
        return_details=False,
    ):
        normalized = []
        for key_type, key_value in candidate_keys:
            nk = self._normalize_key(key_type, key_value)
            if nk:
                normalized.append(nk)

        if not normalized:
            return None if not return_details else {"person_id": None, "merged_people": 0}

        merged_seed = self._merge_seed_fields(normalized, seed_fields=seed_fields)

        if len(normalized) == 1:
            cached = self._cache.get(self._cache_key(*normalized[0]))
            if cached:
                self._touch_person(cached, merged_seed, event_time=event_time)
                return cached if not return_details else {"person_id": cached, "merged_people": 0}

        strong_keys = [(kt, kv) for kt, kv in normalized if self._is_strong_key(kt)]
        matched_person_ids = self.find_person_ids_by_keys(strong_keys)
        merged_people = 0

        if not matched_person_ids and not strong_keys:
            matched_person_ids = self.find_person_ids_by_keys(normalized)

        if matched_person_ids:
            if self._should_merge(normalized, matched_person_ids):
                person_id = self.choose_canonical_person(matched_person_ids)

                for loser_id in matched_person_ids:
                    if loser_id != person_id:
                        self.merge_people(
                            canonical_person_id=person_id,
                            merged_person_id=loser_id,
                            merge_reason=merge_reason,
                            merge_source_table=source_table,
                            merge_source_id=source_id,
                            event_time=event_time,
                        )
                        merged_people += 1
            else:
                person_id = self.choose_canonical_person(matched_person_ids)
        else:
            person_id = self._create_person(merged_seed, event_time=event_time)

        self._touch_person(person_id, merged_seed, event_time=event_time)

        attached_any = False

        for key_type, key_value in normalized:
            returned_id = self.attach_key_to_person(
                person_id=person_id,
                key_type=key_type,
                key_value=key_value,
                source_table=source_table,
                source_id=source_id,
                event_time=event_time,
                follow_existing=False,
            )
            if returned_id:
                attached_any = True
            if returned_id and returned_id != person_id:
                person_id = self.merge_people(
                    canonical_person_id=person_id,
                    merged_person_id=returned_id,
                    merge_reason=merge_reason,
                    merge_source_table=source_table,
                    merge_source_id=source_id,
                    event_time=event_time,
                )
                merged_people += 1

        if not attached_any:
            raise RuntimeError(
                f"Person {person_id} created with zero keys. "
                f"source={source_table}, id={source_id}, keys={normalized}"
            )

        self._touch_person(person_id, merged_seed, event_time=event_time)
        if return_details:
            return {"person_id": person_id, "merged_people": merged_people}
        return person_id

    def resolve_or_create_user_account_person(
        self,
        match_keys,
        attach_keys,
        source_table,
        source_id,
        event_time=None,
        seed_fields=None,
    ):
        candidate_keys = list(match_keys or []) + list(attach_keys or [])
        return self.resolve_or_create_person_from_keys(
            candidate_keys=candidate_keys,
            source_table=source_table,
            source_id=source_id,
            event_time=event_time,
            seed_fields=seed_fields,
            merge_reason="user_account_bridge",
            return_details=True,
        )

    def resolve_or_create_person(
        self,
        key_type: str,
        key_value: str,
        source_table: str,
        source_id: str,
        event_time=None,
    ):
        return self.resolve_or_create_person_from_keys(
            candidate_keys=[(key_type, key_value)],
            source_table=source_table,
            source_id=source_id,
            event_time=event_time,
            seed_fields=None,
        )

    def reassign_event_participants_for_keys(
        self,
        person_id,
        candidate_keys,
        resolution_method="alias_resolved",
    ):
        normalized = []
        for key_type, key_value in candidate_keys:
            nk = self._normalize_key(key_type, key_value)
            if nk:
                normalized.append(nk)

        if not normalized:
            return 0

        conditions = []
        params = {"pid": person_id, "rm": resolution_method}
        for i, (key_type, key_value) in enumerate(normalized):
            conditions.append(f"(raw_key_type = :kt{i} AND raw_key_value = :kv{i})")
            params[f"kt{i}"] = key_type
            params[f"kv{i}"] = key_value

        result = self.db.execute(
            text(
                f"""
                UPDATE {EVENT_PARTICIPANT}
                SET person_id = :pid,
                    resolved_at = NOW()
                WHERE ({' OR '.join(conditions)})
                AND person_id IS DISTINCT FROM :pid
                """
            ),
            {k: v for k, v in params.items() if k != "rm"},
        )
        return result.rowcount or 0

    def backfill_event_participants(self):
        result = self.db.execute(
            text(
                f"""
                UPDATE {EVENT_PARTICIPANT} ep
                SET person_id = ipk.person_id,
                    resolution_method = 'identity_backfill',
                    resolved_at = NOW()
                FROM {IDENTITY_PERSON_KEY} ipk
                WHERE ep.raw_key_type = ipk.key_type
                  AND ep.raw_key_value = ipk.key_value
                  AND ep.person_id IS DISTINCT FROM ipk.person_id
                """
            )
        )
        return result.rowcount or 0
