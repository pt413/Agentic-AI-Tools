from sqlalchemy import text

from app.services.analytics_engine.core.config import SCHEMA_NAME

from .source_db import fetch_all, get_thirdparty_mysql_engine, get_thirdparty_pg_engine
from .sync_base_service import StagingSyncBaseService
from .sync_staging_crm_service import AnalyticsStagingCrmSyncService
from .sync_staging_lifecycle_service import AnalyticsStagingLifecycleSyncService


class AnalyticsStagingSyncService(
    AnalyticsStagingCrmSyncService,
    AnalyticsStagingLifecycleSyncService,
):
    def sync_checkin_form(self, limit=5000, mode="id"):
        table = "checkin_form"
        cp = self.checkpoint.get_checkpoint(table)
        last_id = cp["last_id"]
        last_ts = cp["last_timestamp"]
        source_engine = get_thirdparty_mysql_engine()

        if mode == "time":
            rows = fetch_all(
                source_engine,
                """
                SELECT *
                FROM check_in_form
                WHERE COALESCE(check_in_date, added_on) IS NOT NULL
                  AND (
                        :last_timestamp IS NULL
                        OR COALESCE(check_in_date, added_on) > :last_timestamp
                        OR (COALESCE(check_in_date, added_on) = :last_timestamp AND id > :last_id)
                      )
                ORDER BY COALESCE(check_in_date, added_on), id
                LIMIT :limit
                """,
                {"last_timestamp": last_ts, "last_id": int(last_id or 0), "limit": int(limit)},
            )
        else:
            rows = fetch_all(
                source_engine,
                """
                SELECT *
                FROM check_in_form
                WHERE id > :last_id
                ORDER BY id
                LIMIT :limit
                """,
                {"last_id": int(last_id or 0), "limit": int(limit)},
            )

        if not rows:
            return {"inserted_or_updated": 0, "last_id": last_id, "last_timestamp": last_ts}

        payload = []
        for row in rows:
            r = dict(row)
            ts = self.safe_dt(r.get("check_in_date") or r.get("added_on"))
            payload.append(
                {
                    "source_id": self.safe_int(r.get("id")),
                    "booking_id": self.safe_int(r.get("booking_id")),
                    "user_email": self._clean_lower_email(r.get("email")),
                    "prop_id": self.safe_int(r.get("prop_id")),
                    "prop_name": r.get("prop_name"),
                    "stay_rating": self.safe_numeric(r.get("stay_rating")),
                    "sales_rating": self.safe_numeric(r.get("sales_rating")),
                    "sales_comment": r.get("sales_comment"),
                    "stay_comment": r.get("stay_comment"),
                    "welcome_comment": r.get("welcome_comment") or r.get("welcomeKit_comment"),
                    "welcome_flag": self.safe_bool(r.get("welcome_flag") if "welcome_flag" in r else r.get("welcome")),
                    "linen_flag": self.safe_bool(r.get("linen_flag") if "linen_flag" in r else r.get("linen")),
                    "cleaning_rating": self.safe_numeric(r.get("cleaning_rating") if "cleaning_rating" in r else r.get("cleaning")),
                    "suggestions": r.get("suggestions"),
                    "other_comment": r.get("other_comment"),
                    "supervisor": self._clean_text(r.get("supervisor") or r.get("superviser")),
                    "caretaker": self._clean_text(r.get("caretaker")),
                    "ops_manager": self._clean_text(r.get("ops_manager")),
                    "salesperson": self._clean_text(r.get("salesperson")),
                    "added_on": self.safe_dt(r.get("added_on")),
                    "checkin_date": self.safe_dt(r.get("check_in_date")),
                    "_ts": ts,
                }
            )

        sql = text(
            f"""
            INSERT INTO "{SCHEMA_NAME}".staging_checkin_form (
                source_id, booking_id, user_email, prop_id, prop_name,
                stay_rating, sales_rating, sales_comment, stay_comment, welcome_comment,
                welcome_flag, linen_flag, cleaning_rating, suggestions, other_comment,
                supervisor, caretaker, ops_manager, salesperson,
                added_on, checkin_date, synced_at
            ) VALUES (
                :source_id, :booking_id, :user_email, :prop_id, :prop_name,
                :stay_rating, :sales_rating, :sales_comment, :stay_comment, :welcome_comment,
                :welcome_flag, :linen_flag, :cleaning_rating, :suggestions, :other_comment,
                :supervisor, :caretaker, :ops_manager, :salesperson,
                :added_on, :checkin_date, NOW()
            )
            ON CONFLICT (source_id) DO UPDATE SET
                booking_id = EXCLUDED.booking_id,
                user_email = EXCLUDED.user_email,
                prop_id = EXCLUDED.prop_id,
                prop_name = EXCLUDED.prop_name,
                stay_rating = EXCLUDED.stay_rating,
                sales_rating = EXCLUDED.sales_rating,
                sales_comment = EXCLUDED.sales_comment,
                stay_comment = EXCLUDED.stay_comment,
                welcome_comment = EXCLUDED.welcome_comment,
                welcome_flag = EXCLUDED.welcome_flag,
                linen_flag = EXCLUDED.linen_flag,
                cleaning_rating = EXCLUDED.cleaning_rating,
                suggestions = EXCLUDED.suggestions,
                other_comment = EXCLUDED.other_comment,
                supervisor = EXCLUDED.supervisor,
                caretaker = EXCLUDED.caretaker,
                ops_manager = EXCLUDED.ops_manager,
                salesperson = EXCLUDED.salesperson,
                added_on = EXCLUDED.added_on,
                checkin_date = EXCLUDED.checkin_date,
                synced_at = NOW()
            """
        )

        self._bulk_upsert_in_chunks(sql, payload, 2000)
        new_last_id = max(r["source_id"] for r in payload if r.get("source_id") is not None)
        new_last_ts = max((r["_ts"] for r in payload if r.get("_ts") is not None), default=last_ts)
        self.checkpoint.update_success(table, last_id=new_last_id, last_timestamp=new_last_ts, batch_count=len(payload))
        return {"inserted_or_updated": len(payload), "last_id": new_last_id, "last_timestamp": new_last_ts}

    def sync_checkout_form(self, limit=5000, mode="id"):
        table = "checkout_form"
        cp = self.checkpoint.get_checkpoint(table)
        last_id = cp["last_id"]
        last_ts = cp["last_timestamp"]
        source_engine = get_thirdparty_mysql_engine()

        if mode == "time":
            rows = fetch_all(
                source_engine,
                """
                SELECT *
                FROM check_out_form
                WHERE COALESCE(chk_out_date, added_time) IS NOT NULL
                  AND (
                        :last_timestamp IS NULL
                        OR COALESCE(chk_out_date, added_time) > :last_timestamp
                        OR (COALESCE(chk_out_date, added_time) = :last_timestamp AND id > :last_id)
                      )
                ORDER BY COALESCE(chk_out_date, added_time), id
                LIMIT :limit
                """,
                {"last_timestamp": last_ts, "last_id": int(last_id or 0), "limit": int(limit)},
            )
        else:
            rows = fetch_all(
                source_engine,
                """
                SELECT *
                FROM check_out_form
                WHERE id > :last_id
                ORDER BY id
                LIMIT :limit
                """,
                {"last_id": int(last_id or 0), "limit": int(limit)},
            )

        if not rows:
            return {"inserted_or_updated": 0, "last_id": last_id, "last_timestamp": last_ts}

        payload = []
        for row in rows:
            r = dict(row)
            ts = self.safe_dt(r.get("chk_out_date") or r.get("added_time"))
            payload.append(
                {
                    "source_id": self.safe_int(r.get("id")),
                    "booking_id": self.safe_int(r.get("booking_id")),
                    "user_email": self._clean_lower_email(r.get("email")),
                    "checkout_date": self.safe_dt(r.get("chk_out_date")),
                    "rms_rating": self.safe_numeric(r.get("rms_rating")),
                    "building_rating": self.safe_numeric(r.get("building_rating")),
                    "refer_friends_score": self.safe_numeric(r.get("refer_friends")),
                    "rms_comment": r.get("rms_comment"),
                    "stay_comment": r.get("stay_comment"),
                    "suggestions": r.get("suggestions"),
                    "other_comment": r.get("other_comment"),
                    "supervisor": self._clean_text(r.get("supervisor") or r.get("superviser")),
                    "caretaker": self._clean_text(r.get("caretaker")),
                    "ops_manager": self._clean_text(r.get("ops_manager")),
                    "salesperson": self._clean_text(r.get("salesperson")),
                    "added_time": self.safe_dt(r.get("added_time")),
                    "_ts": ts,
                }
            )

        sql = text(
            f"""
            INSERT INTO "{SCHEMA_NAME}".staging_checkout_form (
                source_id, booking_id, user_email, checkout_date,
                rms_rating, building_rating, refer_friends_score,
                rms_comment, stay_comment, suggestions, other_comment,
                supervisor, caretaker, ops_manager, salesperson, added_time, synced_at
            ) VALUES (
                :source_id, :booking_id, :user_email, :checkout_date,
                :rms_rating, :building_rating, :refer_friends_score,
                :rms_comment, :stay_comment, :suggestions, :other_comment,
                :supervisor, :caretaker, :ops_manager, :salesperson, :added_time, NOW()
            )
            ON CONFLICT (source_id) DO UPDATE SET
                booking_id = EXCLUDED.booking_id,
                user_email = EXCLUDED.user_email,
                checkout_date = EXCLUDED.checkout_date,
                rms_rating = EXCLUDED.rms_rating,
                building_rating = EXCLUDED.building_rating,
                refer_friends_score = EXCLUDED.refer_friends_score,
                rms_comment = EXCLUDED.rms_comment,
                stay_comment = EXCLUDED.stay_comment,
                suggestions = EXCLUDED.suggestions,
                other_comment = EXCLUDED.other_comment,
                supervisor = EXCLUDED.supervisor,
                caretaker = EXCLUDED.caretaker,
                ops_manager = EXCLUDED.ops_manager,
                salesperson = EXCLUDED.salesperson,
                added_time = EXCLUDED.added_time,
                synced_at = NOW()
            """
        )

        self._bulk_upsert_in_chunks(sql, payload, 2000)
        new_last_id = max(r["source_id"] for r in payload if r.get("source_id") is not None)
        new_last_ts = max((r["_ts"] for r in payload if r.get("_ts") is not None), default=last_ts)
        self.checkpoint.update_success(table, last_id=new_last_id, last_timestamp=new_last_ts, batch_count=len(payload))
        return {"inserted_or_updated": len(payload), "last_id": new_last_id, "last_timestamp": new_last_ts}

    # def sync_user_ticket(self, limit=50000, mode="id"):
    #     table = "user_ticket"
    #     cp = self.checkpoint.get_checkpoint(table)
    #     last_id = cp["last_id"]
    #     last_ts = cp["last_timestamp"]
    #     source_engine = get_thirdparty_mysql_engine()

    #     if mode == "time":
    #         rows = fetch_all(
    #             source_engine,
    #             """
    #             SELECT *
    #             FROM user_ticket
    #             WHERE date IS NOT NULL
    #             AND (
    #                     :last_timestamp IS NULL
    #                     OR date > :last_timestamp
    #                     OR (date = :last_timestamp AND id > :last_id)
    #                 )
    #             ORDER BY date, id
    #             LIMIT :limit
    #             """,
    #             {"last_timestamp": last_ts, "last_id": int(last_id or 0), "limit": int(limit)},
    #         )
    #     else:
    #         rows = fetch_all(
    #             source_engine,
    #             """
    #             SELECT *
    #             FROM user_ticket
    #             WHERE id > :last_id
    #             ORDER BY id
    #             LIMIT :limit
    #             """,
    #             {"last_id": int(last_id or 0), "limit": int(limit)},
    #         )

    #     if not rows:
    #         return {"inserted_or_updated": 0, "last_id": last_id, "last_timestamp": last_ts}

    #     payload = []
    #     for row in rows:
    #         r = dict(row)
    #         ts = self.safe_dt(r.get("date"))
    #         payload.append(
    #             {
    #                 "source_id": self.safe_int(r.get("id")),
    #                 "booking_id": self.safe_int(r.get("booking_id")),
    #                 "prop_id": self.safe_int(r.get("prop_id")),
    #                 "building_id": self.safe_int(r.get("building_id")),
    #                 "building_name": r.get("building_name"),
    #                 "category": self._clean_text(r.get("Category") or r.get("category")),
    #                 "priority": self._clean_text(r.get("priority")),
    #                 "description": r.get("description"),
    #                 "mobile_number": self.norm_phone(r.get("mobile_number")),
    #                 "unit_number": self._clean_text(r.get("unit_number")),
    #                 "status": self._clean_text(r.get("status")),
    #                 "reopen_flag": self.safe_int(r.get("reopen_flag")),
    #                 "created_at": self.safe_dt(r.get("date")),
    #                 "assigned_to": self._clean_text(r.get("assign_to") or r.get("assigned_to")),
    #                 "resolved_by": self._clean_text(r.get("resolved_by")),
    #                 "closed_by": self._clean_text(r.get("closed_by")),
    #                 "close_date": self.safe_dt(r.get("close_date")),
    #                 "labour_cost": self.safe_numeric(r.get("labourcost") or r.get("labour_cost")),
    #                 "material_cost": self.safe_numeric(r.get("materialcost") or r.get("material_cost")),
    #                 "total_cost": self.safe_numeric(r.get("total_cost")),
    #                 "active_days": self.safe_int(r.get("active_days")),
    #                 "ticket_rating": self.safe_numeric(r.get("ticket_rating")),
    #                 "ticket_feedback": r.get("ticket_rt_feedback") or r.get("ticket_feedback"),
    #                 "_ts": ts,
    #             }
    #         )

    #     sql = text(
    #         f"""
    #         INSERT INTO "{SCHEMA_NAME}".staging_user_ticket (
    #             source_id, booking_id, prop_id, building_id, building_name,
    #             category, priority, description, mobile_number, unit_number,
    #             status, reopen_flag, created_at, assigned_to, resolved_by, closed_by,
    #             close_date, labour_cost, material_cost, total_cost,
    #             active_days, ticket_rating, ticket_feedback, synced_at
    #         ) VALUES (
    #             :source_id, :booking_id, :prop_id, :building_id, :building_name,
    #             :category, :priority, :description, :mobile_number, :unit_number,
    #             :status, :reopen_flag, :created_at, :assigned_to, :resolved_by, :closed_by,
    #             :close_date, :labour_cost, :material_cost, :total_cost,
    #             :active_days, :ticket_rating, :ticket_feedback, NOW()
    #         )
    #         ON CONFLICT (source_id) DO UPDATE SET
    #             booking_id = EXCLUDED.booking_id,
    #             prop_id = EXCLUDED.prop_id,
    #             building_id = EXCLUDED.building_id,
    #             building_name = EXCLUDED.building_name,
    #             category = EXCLUDED.category,
    #             priority = EXCLUDED.priority,
    #             description = EXCLUDED.description,
    #             mobile_number = EXCLUDED.mobile_number,
    #             unit_number = EXCLUDED.unit_number,
    #             status = EXCLUDED.status,
    #             reopen_flag = EXCLUDED.reopen_flag,
    #             created_at = EXCLUDED.created_at,
    #             assigned_to = EXCLUDED.assigned_to,
    #             resolved_by = EXCLUDED.resolved_by,
    #             closed_by = EXCLUDED.closed_by,
    #             close_date = EXCLUDED.close_date,
    #             labour_cost = EXCLUDED.labour_cost,
    #             material_cost = EXCLUDED.material_cost,
    #             total_cost = EXCLUDED.total_cost,
    #             active_days = EXCLUDED.active_days,
    #             ticket_rating = EXCLUDED.ticket_rating,
    #             ticket_feedback = EXCLUDED.ticket_feedback,
    #             synced_at = NOW()
    #         """
    #     )

    #     self._bulk_upsert_in_chunks(sql, payload, 3000)
    #     new_last_id = max(r["source_id"] for r in payload if r.get("source_id") is not None)
    #     new_last_ts = max((r["_ts"] for r in payload if r.get("_ts") is not None), default=last_ts)
    #     self.checkpoint.update_success(table, last_id=new_last_id, last_timestamp=new_last_ts, batch_count=len(payload))
    #     return {"inserted_or_updated": len(payload), "last_id": new_last_id, "last_timestamp": new_last_ts}

    def sync_user_ticket(self, limit=50000, mode="id"):
        table = "user_ticket"
        cp = self.checkpoint.get_checkpoint(table)
        last_id = cp["last_id"]
        last_ts = cp["last_timestamp"]
        source_engine = get_thirdparty_mysql_engine()

        # ------------------------------------------------------------------
        # 1) Normal incremental sync: fetch only new source IDs.
        #    Do NOT use source date as update cursor because user_ticket has no
        #    updated/updated_at column and date is mostly creation date.
        # ------------------------------------------------------------------
        new_rows = fetch_all(
            source_engine,
            """
            SELECT *
            FROM user_ticket
            WHERE id > :last_id
            ORDER BY id
            LIMIT :limit
            """,
            {
                "last_id": int(last_id or 0),
                "limit": int(limit),
            },
        )

        # ------------------------------------------------------------------
        # 2) Repair sync: re-fetch tickets that analytics still thinks are open.
        #    This catches old tickets whose source status changed to Complete,
        #    Cancelled, Closed, etc. after their original insert.
        # ------------------------------------------------------------------
        repair_limit = int(limit)
        open_ticket_rows = self.db.execute(
            text(
                f"""
                SELECT source_id
                FROM "{SCHEMA_NAME}".staging_user_ticket
                WHERE source_id IS NOT NULL
                AND LOWER(TRIM(COALESCE(status, ''))) IN (
                        'open',
                        'reopen',
                        'waiting'
                )
                ORDER BY source_id
                LIMIT :repair_limit
                """
            ),
            {"repair_limit": repair_limit},
        ).fetchall()

        repair_ids = []
        seen_repair_ids = set()
        for row in open_ticket_rows:
            source_id = getattr(row, "source_id", None)
            if source_id is None and isinstance(row, (tuple, list)) and row:
                source_id = row[0]
            source_id = self.safe_int(source_id)
            if source_id is None or source_id in seen_repair_ids:
                continue
            seen_repair_ids.add(source_id)
            repair_ids.append(source_id)

        repair_rows = []
        if repair_ids:
            placeholders = []
            params = {}
            for idx, source_id in enumerate(repair_ids):
                key = f"id_{idx}"
                placeholders.append(f":{key}")
                params[key] = int(source_id)

            repair_rows = fetch_all(
                source_engine,
                f"""
                SELECT *
                FROM user_ticket
                WHERE id IN ({", ".join(placeholders)})
                ORDER BY id
                """,
                params,
            )

        # ------------------------------------------------------------------
        # 3) Merge new rows + repair rows by source id.
        #    Same source row can appear in both lists; keep one.
        # ------------------------------------------------------------------
        rows_by_id = {}
        for row in list(new_rows or []) + list(repair_rows or []):
            r = dict(row)
            source_id = self.safe_int(r.get("id"))
            if source_id is None:
                continue
            rows_by_id[source_id] = r

        rows = list(rows_by_id.values())

        if not rows:
            return {
                "inserted_or_updated": 0,
                "new_rows": 0,
                "repair_rows": 0,
                "last_id": last_id,
                "last_timestamp": last_ts,
            }

        payload = []
        for r in rows:
            ts = self.safe_dt(r.get("date"))
            payload.append(
                {
                    "source_id": self.safe_int(r.get("id")),
                    "booking_id": self.safe_int(r.get("booking_id")),
                    "prop_id": self.safe_int(r.get("prop_id")),
                    "building_id": self.safe_int(r.get("building_id")),
                    "building_name": r.get("building_name"),
                    "category": self._clean_text(r.get("Category") or r.get("category")),
                    "priority": self._clean_text(r.get("priority")),
                    "description": r.get("description"),
                    "mobile_number": self.norm_phone(r.get("mobile_number")),
                    "unit_number": self._clean_text(r.get("unit_number")),
                    "status": self._clean_text(r.get("status")),
                    "reopen_flag": self.safe_int(r.get("reopen_flag")),
                    "created_at": self.safe_dt(r.get("date")),
                    "assigned_to": self._clean_text(r.get("assign_to") or r.get("assigned_to")),
                    "building_supervisor": self._clean_text(r.get("building_superviser") or r.get("building_supervisor")),
                    "finance_supervisor": self._clean_text(r.get("fin_superviser") or r.get("finance_supervisor")),
                    "building_caretaker": self._clean_text(r.get("building_caretaker")),
                    "coordinator": self._clean_text(r.get("coord_by") or r.get("coordinator")),
                    "team": self._clean_text(r.get("team")),
                    "resolved_by": self._clean_text(r.get("resolved_by")),
                    "closed_by": self._clean_text(r.get("closed_by")),
                    "close_date": self.safe_dt(r.get("close_date")),
                    "labour_cost": self.safe_numeric(r.get("labourcost") or r.get("labour_cost")),
                    "material_cost": self.safe_numeric(r.get("materialcost") or r.get("material_cost")),
                    "total_cost": self.safe_numeric(r.get("total_cost")),
                    "active_days": self.safe_int(r.get("active_days")),
                    "ticket_rating": self.safe_numeric(r.get("ticket_rating")),
                    "ticket_feedback": r.get("ticket_rt_feedback") or r.get("ticket_feedback"),
                    "_ts": ts,
                }
            )

        sql = text(
            f"""
            INSERT INTO "{SCHEMA_NAME}".staging_user_ticket (
                source_id, booking_id, prop_id, building_id, building_name,
                category, priority, description, mobile_number, unit_number,
                status, reopen_flag, created_at, assigned_to,
                building_supervisor, finance_supervisor, building_caretaker, coordinator, team,
                resolved_by, closed_by, close_date, labour_cost, material_cost, total_cost,
                active_days, ticket_rating, ticket_feedback, synced_at
            ) VALUES (
                :source_id, :booking_id, :prop_id, :building_id, :building_name,
                :category, :priority, :description, :mobile_number, :unit_number,
                :status, :reopen_flag, :created_at, :assigned_to,
                :building_supervisor, :finance_supervisor, :building_caretaker, :coordinator, :team,
                :resolved_by, :closed_by, :close_date, :labour_cost, :material_cost, :total_cost,
                :active_days, :ticket_rating, :ticket_feedback, (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            )
            ON CONFLICT (source_id) DO UPDATE SET
                booking_id = EXCLUDED.booking_id,
                prop_id = EXCLUDED.prop_id,
                building_id = EXCLUDED.building_id,
                building_name = EXCLUDED.building_name,
                category = EXCLUDED.category,
                priority = EXCLUDED.priority,
                description = EXCLUDED.description,
                mobile_number = EXCLUDED.mobile_number,
                unit_number = EXCLUDED.unit_number,
                status = EXCLUDED.status,
                reopen_flag = EXCLUDED.reopen_flag,
                created_at = EXCLUDED.created_at,
                assigned_to = EXCLUDED.assigned_to,
                building_supervisor = EXCLUDED.building_supervisor,
                finance_supervisor = EXCLUDED.finance_supervisor,
                building_caretaker = EXCLUDED.building_caretaker,
                coordinator = EXCLUDED.coordinator,
                team = EXCLUDED.team,
                resolved_by = EXCLUDED.resolved_by,
                closed_by = EXCLUDED.closed_by,
                close_date = EXCLUDED.close_date,
                labour_cost = EXCLUDED.labour_cost,
                material_cost = EXCLUDED.material_cost,
                total_cost = EXCLUDED.total_cost,
                active_days = EXCLUDED.active_days,
                ticket_rating = EXCLUDED.ticket_rating,
                ticket_feedback = EXCLUDED.ticket_feedback,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            """
        )

        self._bulk_upsert_in_chunks(sql, payload, 3000)

        # Only advance checkpoint based on genuinely new id rows.
        # Repair rows are old source IDs and should not move the cursor backward/forward.
        new_source_ids = []
        for row in new_rows or []:
            source_id = self.safe_int(dict(row).get("id"))
            if source_id is not None:
                new_source_ids.append(source_id)

        new_last_id = max(new_source_ids, default=int(last_id or 0))
        new_last_ts = max((r["_ts"] for r in payload if r.get("_ts") is not None), default=last_ts)

        self.checkpoint.update_success(
            table,
            last_id=new_last_id,
            last_timestamp=new_last_ts,
            batch_count=len(payload),
        )

        return {
            "inserted_or_updated": len(payload),
            "new_rows": len(new_rows or []),
            "repair_rows": len(repair_rows or []),
            "last_id": new_last_id,
            "last_timestamp": new_last_ts,
        }
 
    def sync_email_messages(self, limit=10000, mode="time"):
        table = "email_messages"
        cp = self.checkpoint.get_checkpoint(table)
        last_id = cp["last_id"]
        last_ts = cp["last_timestamp"]
        source_engine = get_thirdparty_pg_engine()

        rows = fetch_all(
            source_engine,
            """
            SELECT *
            FROM public.emails
            WHERE date IS NOT NULL
              AND (
                    :last_timestamp IS NULL
                    OR date > :last_timestamp
                    OR (date = :last_timestamp AND id > :last_id)
                  )
            ORDER BY date, id
            LIMIT :limit
            """,
            {"last_timestamp": last_ts, "last_id": int(last_id or 0), "limit": int(limit)},
        )

        if not rows:
            return {"inserted_or_updated": 0, "last_id": last_id, "last_timestamp": last_ts}

        payload = []
        for row in rows:
            r = dict(row)
            payload.append(
                {
                    "source_id": self.safe_int(r.get("id")),
                    "msgid": self._clean_text(r.get("msgid")),
                    "subject": r.get("subject"),
                    "direction": self._clean_text(r.get("direction")),
                    "sender": r.get("sender"),
                    "receiver": r.get("receiver"),
                    "email_date": self.safe_dt(r.get("date")),
                    "body": r.get("body"),
                    "snippet": r.get("snippet"),
                    "thread_id": self._clean_text(r.get("thread_id")),
                }
            )

        sql = text(
            f"""
            INSERT INTO "{SCHEMA_NAME}".staging_email_messages (
                source_id, msgid, subject, direction, sender, receiver,
                email_date, body, snippet, thread_id, synced_at
            ) VALUES (
                :source_id, :msgid, :subject, :direction, :sender, :receiver,
                :email_date, :body, :snippet, :thread_id, NOW()
            )
            ON CONFLICT (source_id) DO UPDATE SET
                msgid = EXCLUDED.msgid,
                subject = EXCLUDED.subject,
                direction = EXCLUDED.direction,
                sender = EXCLUDED.sender,
                receiver = EXCLUDED.receiver,
                email_date = EXCLUDED.email_date,
                body = EXCLUDED.body,
                snippet = EXCLUDED.snippet,
                thread_id = EXCLUDED.thread_id,
                synced_at = NOW()
            """
        )

        self._bulk_upsert_in_chunks(sql, payload, 3000)
        new_last_id = max(r["source_id"] for r in payload if r.get("source_id") is not None)
        new_last_ts = max((r["email_date"] for r in payload if r.get("email_date") is not None), default=last_ts)
        self.checkpoint.update_success(table, last_id=new_last_id, last_timestamp=new_last_ts, batch_count=len(payload))
        return {"inserted_or_updated": len(payload), "last_id": new_last_id, "last_timestamp": new_last_ts}

    def run_sync(self, sync_name: str, limit: int, mode: str | None = None):
        registry = {
            "user_account": (self.sync_user_accounts, "id"),
            # Reconciles only existing PostgreSQL caretakers against MySQL team.
            "admin_user_account": (self.sync_admin_user_accounts, "daily"),
            "lead_tracking": (self.sync_leads, "id"),
            "call_log_tracking": (self.sync_call_logs, "id"),
            "site_visits": (self.sync_site_visits, "id"),
            "travel_cart": (self.sync_travel_cart, "id"),
            "user_wishlist": (self.sync_wishlist, "time"),
            "booking_confirm": (self.sync_booking_confirm, "id"),
            "user_contact_info": (self.sync_user_contact_info, "id"),
            "whatsapp_messages": (self.sync_whatsapp_messages, "time"),
            "web_visits": (self.sync_web_visits, "id"),
            "checkin_form": (self.sync_checkin_form, "id"),
            "checkout_form": (self.sync_checkout_form, "id"),
            "user_ticket": (self.sync_user_ticket, "id"),
            "email_messages": (self.sync_email_messages, "time"),
            "booking_audit_history": (self.sync_booking_audit_history, "id"),
            "booking_invoice_details": (self.sync_booking_invoice_details, "id"),
        }

        if sync_name not in registry:
            raise ValueError(f"Unsupported sync_name={sync_name}")

        fn, default_mode = registry[sync_name]
        return fn(limit=limit, mode=mode or default_mode)
