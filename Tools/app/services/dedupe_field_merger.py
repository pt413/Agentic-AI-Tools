"""Phase 5: Field merging and activity reassignment"""
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from app.dedupe_config import TZ
from app.model.user_data_test import UserDataa
from app.model.user_merge_log import UserMergeLog
from app.utils.dedupe_logger import log

class FieldMerger:
    @staticmethod
    def merge_fields(session, master_id, dup_id, ctx):
        """Merge fields from duplicate into master"""
        log.debug(f"[PHASE_5] Merging fields: master={master_id}, dup={dup_id}")

        master = session.get(UserDataa, master_id)
        dup = session.get(UserDataa, dup_id)

        if not master or not dup:
            raise ValueError(f"User not found: master={master_id}, dup={dup_id}")

        snapshot = {}

        if not master.name and dup.name:
            master.name = dup.name
            snapshot["name"] = dup.name

        if (not master.role or master.role.lower() == "customer") and dup.role:
            if dup.role.lower() == "admin":
                master.role = dup.role
                snapshot["role"] = dup.role

        if dup.phone:
            if not master.phone:
                master.phone = dup.phone
                snapshot["phone"] = dup.phone
            else:
                snapshot["phone_conflict"] = {"kept": master.phone, "discarded": dup.phone}
            dup.phone = None

        if dup.email:
            if not master.email:
                master.email = dup.email
                snapshot["email"] = dup.email
            else:
                snapshot["email_conflict"] = {"kept": master.email, "discarded": dup.email}
            dup.email = None

        if dup.wa_num:
            if not master.wa_num:
                master.wa_num = dup.wa_num
                snapshot["wa_num"] = dup.wa_num
            else:
                snapshot["wa_num_conflict"] = {"kept": master.wa_num, "discarded": dup.wa_num}
            dup.wa_num = None

        master.updation_time = datetime.now(TZ)
        
        sender_count = session.execute(
            text("UPDATE uni_activity_merge_test SET sen_id = :master WHERE sen_id = :dup"),
            {"master": master_id, "dup": dup_id}
        ).rowcount or 0

        receiver_count = session.execute(
            text("UPDATE uni_activity_merge_test SET rec_id = :master WHERE rec_id = :dup"),
            {"master": master_id, "dup": dup_id}
        ).rowcount or 0

        activities_updated = sender_count + receiver_count
        snapshot["activities_reassigned"] = activities_updated
        log.info(f"[PHASE_5] Reassigned: sender={sender_count}, receiver={receiver_count}, total={activities_updated}")

        entry = UserMergeLog(
            master_id=master_id,
            merged_id=dup_id,
            fields_merged={**snapshot, "execution_context": ctx.to_dict()},
            activities_reassigned=activities_updated,
            merged_at=datetime.now(TZ),
            reason="dedupe_v7"
        )
        session.add(entry)
        session.flush()

        session.delete(dup)
        session.flush()

        return snapshot, activities_updated