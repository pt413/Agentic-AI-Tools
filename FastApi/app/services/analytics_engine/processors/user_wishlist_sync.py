from sqlalchemy import text

from ..core.service_container import ServiceContainer
from ..core.config import STAGING_WISHLIST
from ..core.utils import normalize_email, normalize_phone
from .time_cursor_sync_processor import TimeCursorSyncProcessor


class UserWishlistSync(TimeCursorSyncProcessor):
    PROCESSOR_NAME = "user_wishlist_sync"
    SOURCE_TABLE = STAGING_WISHLIST
    SOURCE_TABLE_NAME = "staging_user_wishlist"

    def __init__(self, db):
        super().__init__(db, ServiceContainer(db), default_batch_size=5000)

    def fetch_rows_with_time_cursor(self, last_source_id, last_timestamp, batch_size: int):
        return self.source.fetch_user_wishlist(
            last_source_id=last_source_id,
            last_timestamp=last_timestamp,
            batch_size=batch_size,
        )

    def get_row_checkpoint_timestamp(self, row, row_result=None):
        return getattr(row, "added_on", None) or getattr(row, "synced_at", None)

    def _lookup_user_account(self, user_id):
        if user_id in (None, ""):
            return None

        row = self.db.execute(
            text(
                """
                SELECT
                    source_id,
                    username,
                    email,
                    normalized_phone,
                    phone_number
                FROM "AnalyticsEngine".staging_user_account
                WHERE source_id = :user_id
                LIMIT 1
                """
            ),
            {"user_id": int(user_id)},
        ).fetchone()
        return row

    def _build_wishlist_identity(self, row):
        user_id = getattr(row, "user_id", None)
        account = self._lookup_user_account(user_id)

        normalized_username = None
        normalized_email = None
        normalized_phone = None
        candidate_keys = []

        if account is not None:
            username = getattr(account, "username", None)
            if username is not None:
                username = str(username).strip().lower()
                if username:
                    normalized_username = username
                    candidate_keys.append(("username", normalized_username))

            normalized_email = normalize_email(getattr(account, "email", None))
            if normalized_email:
                candidate_keys.append(("email", normalized_email))

            normalized_phone = normalize_phone(
                getattr(account, "normalized_phone", None) or getattr(account, "phone_number", None)
            )
            if normalized_phone:
                candidate_keys.append(("phone", normalized_phone))

        deduped_keys = []
        seen = set()
        for key_type, key_value in candidate_keys:
            item = (str(key_type).strip().lower(), str(key_value).strip())
            if not item[1] or item in seen:
                continue
            seen.add(item)
            deduped_keys.append(item)

        return {
            "account": account,
            "candidate_keys": deduped_keys,
            "username": normalized_username,
            "email": normalized_email,
            "phone": normalized_phone,
        }

    def _pick_participant_key(self, identity_details):
        if identity_details.get("username"):
            return "username", identity_details["username"]
        if identity_details.get("email"):
            return "email", identity_details["email"]
        if identity_details.get("phone"):
            return "phone", identity_details["phone"]
        return None, None

    def process_row(self, r):
        event_time = getattr(r, "added_on", None) or getattr(r, "synced_at", None)
        if not event_time:
            return {
                "processed": 1,
                "last_source_id": getattr(r, "source_id", None),
            }

        identity_details = self._build_wishlist_identity(r)
        candidate_keys = identity_details["candidate_keys"]
        person_id = None
        merged_people = 0
        repaired_participants = 0

        if candidate_keys:
            result = self.identity.resolve_or_create_person_from_keys(
                candidate_keys=candidate_keys,
                source_table=self.SOURCE_TABLE_NAME,
                source_id=str(r.source_id),
                event_time=event_time,
                seed_fields={
                    "canonical_name": identity_details.get("username"),
                    "primary_email": identity_details.get("email"),
                    "primary_phone": identity_details.get("phone"),
                },
                merge_reason="wishlist_user_bridge",
                return_details=True,
            )
            person_id = result.get("person_id") if isinstance(result, dict) else result
            merged_people = int(result.get("merged_people", 0)) if isinstance(result, dict) else 0
            if person_id:
                repaired_participants = self.identity.reassign_event_participants_for_keys(
                    person_id=person_id,
                    candidate_keys=candidate_keys,
                )

        event_meta = {
            "source_id": getattr(r, "source_id", None),
            "user_id": getattr(r, "user_id", None),
            "prop_id": getattr(r, "prop_id", None),
            "added_on": event_time,
            "person_id": person_id,
            "wishlist_username": identity_details.get("username"),
            "wishlist_email": identity_details.get("email"),
            "wishlist_phone": identity_details.get("phone"),
            "merged_people": merged_people,
        }
        event_meta = {k: v for k, v in event_meta.items() if v is not None}

        event_id = self.events.create_event(
            event_family="engagement",
            event_name="wishlist_added",
            event_channel="wishlist",
            event_direction="inbound",
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

        contexts_written = 0
        created_events = 1 if event_id else 0
        participants_written = 0

        if event_id:
            user_id = getattr(r, "user_id", None)
            prop_id = getattr(r, "prop_id", None)

            if user_id:
                cid = self.context.add_context(event_id, "user", str(user_id))
                contexts_written += 1 if cid else 0

            if prop_id:
                cid = self.context.add_context(event_id, "property", str(prop_id))
                contexts_written += 1 if cid else 0

            participant_key_type, participant_key_value = self._pick_participant_key(identity_details)
            if participant_key_type and participant_key_value:
                participant_id = self.participants.add_participant(
                    event_id=event_id,
                    participant_seq=1,
                    key_type=participant_key_type,
                    key_value=str(participant_key_value),
                    role="customer",
                    direction_role="from",
                    source_table=self.SOURCE_TABLE_NAME,
                    source_id=r.source_id,
                    event_time=event_time,
                )
                participants_written += 1 if participant_id else 0

        return {
            "processed": 1,
            "last_source_id": getattr(r, "source_id", None),
            "last_timestamp": event_time,
            "created_events": created_events,
            "contexts_written": contexts_written,
            "participants_written": participants_written,
            "merged_people": merged_people,
            "repaired_participants": repaired_participants,
        }

    def run(self, batch_size=5000, limit=None, start_source_id=None):
        result = super().run(
            batch_size=batch_size,
            limit=limit,
            start_source_id=start_source_id,
        )
        return {
            "processed_wishlist_rows": result["processed_rows"],
            "created_events": result.get("created_events", 0),
            "contexts_written": result.get("contexts_written", 0),
            "participants_written": result.get("participants_written", 0),
            "repaired_participants": result.get("repaired_participants", 0),
            "merged_people": result.get("merged_people", 0),
            "last_source_id": result["last_source_id"],
        }
