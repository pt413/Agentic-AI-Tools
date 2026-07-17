from ..processors.base_sync_processor import BaseSyncProcessor
from ..core.service_container import ServiceContainer
from ..core.config import STAGING_USERS
from ..core.utils import normalize_email, normalize_phone


class UserAccountSync(BaseSyncProcessor):
    PROCESSOR_NAME = "user_account_sync"
    SOURCE_TABLE = STAGING_USERS
    SOURCE_TABLE_NAME = "staging_user_account"

    def __init__(self, db):
        super().__init__(db, ServiceContainer(db), default_batch_size=5000)

    def fetch_rows(self, last_source_id: int, batch_size: int):
        return self.source.fetch_user_accounts(
            last_source_id=last_source_id,
            batch_size=batch_size,
        )

    def _normalize_text(self, value):
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    def _normalize_bool_like(self, value):
        if value is None:
            return None
        text = str(value).strip().lower()
        if text in {"1", "true", "t", "yes", "y", "active", "enabled"}:
            return True
        if text in {"0", "false", "f", "no", "n", "inactive", "disabled"}:
            return False
        return None

    def _derive_event_status(self, active_value):
        active_flag = self._normalize_bool_like(active_value)
        if active_flag is True:
            return "active"
        if active_flag is False:
            return "inactive"
        return None

    def _build_event_meta(
        self,
        r,
        person_id,
        merged_people,
        normalized_username,
        normalized_email,
        normalized_phone,
    ):
        last_login_time = getattr(r, "last_login_time", None)
        created_on = getattr(r, "created_on", None)
        synced_at = getattr(r, "synced_at", None)

        return {
            "person_id": person_id,
            "username": normalized_username,
            "email": normalized_email,
            "phone_number": normalized_phone,
            "team": self._normalize_text(getattr(r, "team", None)),
            "is_admin": self._normalize_text(getattr(r, "is_admin", None)),
            "person_kind": self._derive_person_kind(r),
            "active": self._normalize_bool_like(getattr(r, "active", None)),
            "created_on": created_on.isoformat() if created_on else None,
            "last_login_time": last_login_time.isoformat() if last_login_time else None,
            "synced_at": synced_at.isoformat() if synced_at else None,
            "merged_people": merged_people,
        }

    def process_row(self, r):
        candidate_keys = self.source.build_user_account_candidate_keys(r)

        normalized_username = self._normalize_text(getattr(r, "username", None))
        normalized_email = normalize_email(getattr(r, "email", None))
        normalized_phone = normalize_phone(
            getattr(r, "normalized_phone", None) or getattr(r, "phone_number", None)
        )

        if not candidate_keys:
            return {
                "processed": 1,
                "last_source_id": r.source_id,
            }

        created_on = getattr(r, "created_on", None)
        synced_at = getattr(r, "synced_at", None)
        event_time = created_on or synced_at

        person_kind = self._derive_person_kind(r)

        seed_fields = {
            "canonical_name": normalized_username,
            "primary_email": normalized_email,
            "primary_phone": normalized_phone,
            "person_kind": person_kind,
            "kind_confidence": 1,
        }

        result = self.identity.resolve_or_create_person_from_keys(
            candidate_keys=candidate_keys,
            source_table=self.SOURCE_TABLE_NAME,
            source_id=str(r.source_id),
            event_time=event_time,
            seed_fields=seed_fields,
            merge_reason="user_account_bridge",
            return_details=True,
        )

        person_id = result["person_id"] if isinstance(result, dict) else result
        merged_people = int(result.get("merged_people", 0)) if isinstance(result, dict) else 0

        repaired_participants = 0
        created_events = 0
        participants_written = 0

        if not person_id:
            return {
                "processed": 1,
                "last_source_id": r.source_id,
                "merged_people": merged_people,
                "repaired_participants": repaired_participants,
                "created_events": created_events,
                "participants_written": participants_written,
            }

        repaired_participants = self.identity.reassign_event_participants_for_keys(
            person_id=person_id,
            candidate_keys=candidate_keys,
        )

        if event_time is None:
            return {
                "processed": 1,
                "last_source_id": r.source_id,
                "merged_people": merged_people,
                "repaired_participants": repaired_participants,
                "created_events": created_events,
                "participants_written": participants_written,
            }

        event_meta = self._build_event_meta(
            r=r,
            person_id=person_id,
            merged_people=merged_people,
            normalized_username=normalized_username,
            normalized_email=normalized_email,
            normalized_phone=normalized_phone,
        )

        event_id = self.events.create_event(
            event_family="identity",
            event_name="user_account_created",
            event_channel="user_account",
            event_direction="system",
            event_time=event_time,
            event_end_time=None,
            event_status=None,
            source_table=self.SOURCE_TABLE_NAME,
            source_id=str(r.source_id),
            metric_value=None,
            metric_unit=None,
            metric_name=None,
            meta=event_meta,
        )

        if event_id:
            created_events = 1

            participant_key_type = None
            participant_key_value = None

            if normalized_email:
                participant_key_type = "email"
                participant_key_value = normalized_email
            elif normalized_phone:
                participant_key_type = "phone"
                participant_key_value = normalized_phone
            elif normalized_username:
                participant_key_type = "username"
                participant_key_value = normalized_username

            if participant_key_value:
                event_participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=1,
                    key_type=participant_key_type,
                    key_value=str(participant_key_value),
                    role="account_holder",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                if event_participant_id:
                    participants_written += 1

        return {
            "processed": 1,
            "last_source_id": r.source_id,
            "merged_people": merged_people,
            "repaired_participants": repaired_participants,
            "created_events": created_events,
            "participants_written": participants_written,
        }
    def run(self, batch_size=5000, limit=None, start_source_id=None):
        result = super().run(
            batch_size=batch_size,
            limit=limit,
            start_source_id=start_source_id,
        )
        return {
            "processed_user_rows": result["processed_rows"],
            "created_events": result.get("created_events", 0),
            "participants_written": result.get("participants_written", 0),
            "repaired_participants": result.get("repaired_participants", 0),
            "merged_people": result.get("merged_people", 0),
            "last_source_id": result["last_source_id"],
        }
        
    def _derive_person_kind(self, r):
        is_admin = self._normalize_text(getattr(r, "is_admin", None))
        team = self._normalize_text(getattr(r, "team", None))

        def norm(value):
            if value is None:
                return None
            text = str(value).strip().lower()
            if not text or text in {"0", "na", "n/a", "null", "none"}:
                return None
            out = []
            last_sep = False
            for ch in text:
                if ch.isalnum():
                    out.append(ch)
                    last_sep = False
                else:
                    if not last_sep:
                        out.append("_")
                        last_sep = True
            return "".join(out).strip("_") or None

        is_admin_norm = norm(is_admin)
        team_norm = norm(team)

        external_markers = {
            "user", "customer", "guest", "tenant", "resident", "lead", "prospect"
        }

        internal_role_markers = {
            "admin", "employee", "staff", "agent", "manager", "executive",
            "sales", "ops", "operations", "finance", "support", "marketing"
        }

        internal_team_markers = {
            "sales", "sales_team",
            "ops", "ops_team", "operations",
            "finance", "marketing", "support", "admin"
        }

        if is_admin_norm in external_markers:
            return "external"

        if is_admin_norm in internal_role_markers:
            return "internal"

        if team_norm in internal_team_markers:
            return "internal"

        # if team is populated at all, it is usually an internal user signal
        if team_norm:
            return "internal"

        return "unknown"
