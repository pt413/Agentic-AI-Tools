"""Phase 9: Merge duplicate groups"""
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text
from app.services.dedupe_master_selector import MasterSelector
from app.services.dedupe_field_merger import FieldMerger
from app.services.dedupe_locking import AdvisoryLock
from app.utils.dedupe_logger import log

class GroupMerger:
    @staticmethod
    def merge_duplicate(session, master_id, dup_id, ctx):
        """Merge single duplicate into master"""
        log.debug(f"[MERGE_DUP] Starting: master={master_id}, dup={dup_id}")
        sp = session.begin_nested()

        try:
            existing = session.execute(
                text("SELECT merged_id FROM user_merge_log WHERE merged_id = :mid LIMIT 1"),
                {"mid": dup_id}
            ).first()

            if existing:
                log.info(f"[MERGE_DUP] Already merged: dup={dup_id}")
                sp.rollback()
                return False

            snapshot, activities = FieldMerger.merge_fields(session, master_id, dup_id, ctx)

            sp.commit()
            log.info(f"[MERGE_DUP] Complete: master={master_id}, dup={dup_id}, activities={activities}")
            return True

        except IntegrityError as e:
            sp.rollback()
            if "already_merged" in str(e).lower() or "user_merge_log" in str(e):
                log.warning(f"[MERGE_DUP] Already merged (error): dup={dup_id}")
                return False
            log.exception(f"[MERGE_DUP] Integrity error")
            return False
        except Exception as e:
            sp.rollback()
            log.exception(f"[MERGE_DUP] Failed: master={master_id}, dup={dup_id}")
            return False

    @staticmethod
    def merge_group(session, group, ctx):
        """Merge entire duplicate group"""
        log.info(f"[MERGE_GROUP] Starting: group_size={len(group)}, group={group}")
        
        if len(group) < 2:
            log.warning(f"[MERGE_GROUP] Group too small: {len(group)}")
            return {"error": "group_too_small", "group": group}

        key = AdvisoryLock.compute_key(group)

        try:
            AdvisoryLock.acquire(session, key, max_retries=3)
            log.debug(f"[MERGE_GROUP] Lock acquired")

            try:
                master_id = MasterSelector.pick_master(session, group)
            except Exception as e:
                log.error(f"[MERGE_GROUP] Failed to pick master: {type(e).__name__}")
                raise
            
            log.info(f"[MERGE_GROUP] Master selected: {master_id}")
            
            merged_ids = []

            for i, dup_id in enumerate(group):
                if dup_id == master_id:
                    continue

                try:
                    success = GroupMerger.merge_duplicate(session, master_id, dup_id, ctx)
                    if success:
                        merged_ids.append(dup_id)
                except Exception as e:
                    log.warning(f"[MERGE_GROUP] Failed to merge {dup_id}: {type(e).__name__}")
                    continue

            try:
                session.commit()
                log.info(f"[MERGE_GROUP] Committed: master={master_id}, merged={len(merged_ids)}/{len(group)-1}")
            except Exception as e:
                log.error(f"[MERGE_GROUP] Commit failed: {type(e).__name__}")
                session.rollback()
                raise

            return {
                "master_id": master_id,
                "merged_ids": merged_ids,
                "group_size": len(group),
                "merged_count": len(merged_ids),
                "success": len(merged_ids) > 0
            }

        except Exception as e:
            session.rollback()
            log.error(f"[MERGE_GROUP] Group merge failed: group={group}")
            return {"error": str(e), "group": group, "success": False}

        finally:
            try:
                AdvisoryLock.release(session, key)
            except Exception as e:
                log.warning(f"[MERGE_GROUP] Lock release failed")