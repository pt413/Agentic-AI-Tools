"""Phase 4: Master selection with smart scoring"""
from datetime import datetime
from sqlalchemy import text
from app.dedupe_config import MASTER_WEIGHTS, TZ
from app.model.user_data_test import UserDataa
from app.utils.dedupe_logger import log

class MasterSelector:
    @staticmethod
    def get_activity_stats(session, user_ids):
        """Fetch activity stats for users"""
        log.debug(f"[PHASE_4] Fetching activity stats for {len(user_ids)} users")
        
        stats = {uid: {"activity_count": 0, "last_activity": None} for uid in user_ids}

        if not user_ids:
            return stats

        sql = text("""
            WITH stats AS (
                SELECT sen_id AS uid, COUNT(*) AS cnt, MAX(timestamp) AS ts
                FROM uni_activity_merge_test WHERE sen_id = ANY(:uids) GROUP BY sen_id
                UNION ALL
                SELECT rec_id AS uid, COUNT(*) AS cnt, MAX(timestamp) AS ts
                FROM uni_activity_merge_test WHERE rec_id = ANY(:uids) GROUP BY rec_id
            )
            SELECT uid, SUM(cnt) AS total, MAX(ts) AS last_ts FROM stats GROUP BY uid
        """)

        for uid, total, last_ts in session.execute(sql, {"uids": user_ids}):
            stats[uid]["activity_count"] = int(total or 0)
            stats[uid]["last_activity"] = last_ts

        return stats

    @staticmethod
    def compute_score(user, stats):
        """Compute master selection score"""
        score = 0.0

        if getattr(user, "role", "").lower() == "admin":
            score += MASTER_WEIGHTS["role_admin"]

        id_count = sum([bool(user.phone), bool(user.email), bool(user.wa_num)])
        score += id_count * MASTER_WEIGHTS["identifier_count"]

        if getattr(user, "name", None):
            score += MASTER_WEIGHTS["name_present"]

        activity_count = stats.get("activity_count", 0)
        if activity_count is not None:
            activity_count = int(activity_count)
        score += activity_count * MASTER_WEIGHTS["activity_unit"]

        last_activity = stats.get("last_activity")
        if last_activity:
            try:
                if isinstance(last_activity, datetime) and last_activity.tzinfo is None:
                    last_activity = last_activity.replace(tzinfo=TZ)
                age_days = (datetime.now(TZ) - last_activity).days
                recency = max(0.0, MASTER_WEIGHTS["recency_days_cap"] - age_days)
                score += recency
            except Exception as e:
                log.warning(f"[PHASE_4] Recency score error: {type(e).__name__}")

        return score

    @staticmethod
    def pick_master(session, group):
        """Select master from duplicate group"""
        log.debug(f"[PHASE_4] Picking master from group of {len(group)} users")
        
        users = session.query(UserDataa).filter(UserDataa.u_id.in_(group)).order_by(UserDataa.u_id).all()

        if not users:
            raise ValueError(f"No users found for group {group}")

        stats_map = MasterSelector.get_activity_stats(session, group)
        best_user, best_score = None, -1.0

        for user in users:
            stats = stats_map.get(user.u_id, {})
            score = MasterSelector.compute_score(user, stats)
            log.debug(f"[PHASE_4] User {user.u_id}: score={score:.2f}")

            if score > best_score or (score == best_score and user.u_id < best_user.u_id):
                best_user = user
                best_score = score

        log.info(f"[PHASE_4] Master selected: u_id={best_user.u_id}, score={best_score:.2f}")
        return best_user.u_id