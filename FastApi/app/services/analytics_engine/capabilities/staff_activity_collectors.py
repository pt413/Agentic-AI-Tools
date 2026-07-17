from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence

from .staff_activity_common import *
from .staff_activity_common import StaffActivityBaseService, _safe_ident


class StaffActivityCollectorService(StaffActivityBaseService):
    def _row_ref_match(self, row: dict[str, Any], columns: Sequence[str], refs: set[str]) -> bool:
        for column in columns:
            if lower_ref(row.get(column)) in refs:
                return True
        return False

    def collect_tickets(
        self,
        staff: dict[str, Any],
        buildings: Sequence[dict[str, Any]],
        properties: Sequence[dict[str, Any]],
        start_dt: datetime,
        end_dt: datetime,
        limit: int,
        *,
        include_scope: bool = False,
    ) -> list[dict[str, Any]]:
        """Collect tickets closed/resolved/completed inside the requested window.

        By default this is own-name only: assigned_to/resolved_by/closed_by or
        source ticket owner refs such as building_caretaker/building_supervisor/
        finance_supervisor/coordinator must match the staff member. Broad
        building/property scoped tickets are opt-in because broad building
        matches can otherwise pull unrelated tickets.
        """
        table_name = "staging_user_ticket"
        if not self.table_exists(table_name):
            return []
        columns = self.table_columns(table_name)
        refs = set(self.staff_refs(staff))
        building_ids = {str(row.get("building_id") or row.get("source_id")) for row in buildings if row.get("building_id") or row.get("source_id")}
        prop_ids = {str(row.get("prop_id") or row.get("source_id")) for row in properties if row.get("prop_id") or row.get("source_id")}

        conds: list[str] = []
        params: dict[str, Any] = {"start_dt": start_dt, "end_dt": end_dt, "limit_n": int(limit)}
        ref_columns = [
            c for c in (
                "assigned_to",
                "resolved_by",
                "closed_by",
                "building_caretaker",
                "building_supervisor",
                "finance_supervisor",
                "coordinator",
            )
            if c in columns
        ]
        if refs and ref_columns:
            in_sql, in_params = build_in_params(sorted(refs), "ref")
            params.update(in_params)
            conds.extend([f"LOWER(TRIM(COALESCE(t.{col}::text, ''))) IN {in_sql}" for col in ref_columns])
        if include_scope and building_ids and "building_id" in columns:
            in_sql, in_params = build_in_params(sorted(building_ids), "tbid")
            params.update(in_params)
            conds.append(f"t.building_id::text IN {in_sql}")
        if include_scope and prop_ids and "prop_id" in columns:
            in_sql, in_params = build_in_params(sorted(prop_ids), "tpid")
            params.update(in_params)
            conds.append(f"t.prop_id::text IN {in_sql}")
        if not conds:
            return []

        closed_conds: list[str] = []
        if "status" in columns:
            status_sql, status_params = build_in_params(sorted(CLOSED_TICKET_STATUSES), "tstatus")
            params.update(status_params)
            closed_conds.append(f"LOWER(TRIM(COALESCE(t.status::text, ''))) IN {status_sql}")
        if "close_date" in columns:
            closed_conds.append("t.close_date IS NOT NULL")
        if not closed_conds:
            return []
        closed_sql = " OR ".join(f"({cond})" for cond in closed_conds)

        # Use close_date first so the requested days means 'closed during this period'.
        time_expr = self.coalesce_existing(table_name, "t", ["close_date", "synced_at", "created_at"])
        time_filter = f"AND {time_expr} >= :start_dt AND {time_expr} < :end_dt" if time_expr != "NULL" else ""
        select_columns = [
            "source_id", "booking_id", "prop_id", "building_id", "building_name", "category", "priority",
            "description", "mobile_number", "unit_number", "status", "reopen_flag", "created_at",
            "assigned_to", "building_caretaker", "building_supervisor", "finance_supervisor",
            "coordinator", "team", "resolved_by", "closed_by", "close_date", "labour_cost", "material_cost",
            "total_cost", "active_days", "ticket_rating", "ticket_feedback", "synced_at",
        ]
        select_list = self.select_exprs(table_name, "t", select_columns)
        where_sql = " OR ".join(f"({cond})" for cond in conds)
        rows = self.rows(
            f"""
            SELECT {select_list}, {time_expr} AS activity_time
            FROM {table_ref(self.schema, table_name)} t
            WHERE ({where_sql})
              AND ({closed_sql})
              {time_filter}
            ORDER BY activity_time DESC NULLS LAST, t.source_id DESC NULLS LAST
            LIMIT :limit_n
            """,
            params,
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            status = lower_ref(row.get("status")) or ""
            if status not in CLOSED_TICKET_STATUSES and not row.get("close_date"):
                continue
            match_reasons: list[str] = []
            if self._row_ref_match(row, ("assigned_to",), refs):
                match_reasons.append("assigned_to_staff")
            if self._row_ref_match(row, ("building_caretaker",), refs):
                match_reasons.append("building_caretaker_owner")
            if self._row_ref_match(row, ("building_supervisor",), refs):
                match_reasons.append("building_supervisor_owner")
            if self._row_ref_match(row, ("finance_supervisor",), refs):
                match_reasons.append("finance_supervisor_owner")
            if self._row_ref_match(row, ("coordinator",), refs):
                match_reasons.append("coordinator_owner")
            if self._row_ref_match(row, ("resolved_by",), refs):
                match_reasons.append("resolved_by_staff")
            if self._row_ref_match(row, ("closed_by",), refs):
                match_reasons.append("closed_by_staff")
            if str(row.get("building_id")) in building_ids:
                match_reasons.append("assigned_building")
            if str(row.get("prop_id")) in prop_ids:
                match_reasons.append("assigned_property")
            row["match_reasons"] = match_reasons
            row["ticket_state"] = "closed"
            out.append(compact_dict(row))
        return out

    def collect_checkin_feedback(
        self,
        staff: dict[str, Any],
        properties: Sequence[dict[str, Any]],
        start_dt: datetime,
        end_dt: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        table_name = "staging_checkin_form"
        if not self.table_exists(table_name):
            return []
        columns = self.table_columns(table_name)
        refs = set(self.staff_refs(staff))
        prop_ids = {str(row.get("prop_id") or row.get("source_id")) for row in properties if row.get("prop_id") or row.get("source_id")}
        conds: list[str] = []
        params: dict[str, Any] = {"start_dt": start_dt, "end_dt": end_dt, "limit_n": int(limit)}
        ref_columns = [col for col in ("caretaker", "supervisor", "ops_manager", "salesperson") if col in columns]
        if refs and ref_columns:
            in_sql, in_params = build_in_params(sorted(refs), "ciref")
            params.update(in_params)
            conds.extend([f"LOWER(TRIM(COALESCE(c.{_safe_ident(col)}::text, ''))) IN {in_sql}" for col in ref_columns])
        if prop_ids and "prop_id" in columns:
            in_sql, in_params = build_in_params(sorted(prop_ids), "ciprop")
            params.update(in_params)
            conds.append(f"c.prop_id::text IN {in_sql}")
        if not conds:
            return []
        time_expr = self.coalesce_existing(table_name, "c", ["checkin_date", "added_on", "synced_at"])
        time_filter = f"AND {time_expr} >= :start_dt AND {time_expr} < :end_dt" if time_expr != "NULL" else ""
        select_columns = [
            "source_id", "booking_id", "user_email", "prop_id", "prop_name", "stay_rating", "sales_rating",
            "sales_comment", "stay_comment", "welcome_comment", "welcome_flag", "linen_flag", "cleaning_rating",
            "suggestions", "other_comment", "supervisor", "caretaker", "ops_manager", "salesperson",
            "added_on", "checkin_date", "synced_at",
        ]
        select_list = self.select_exprs(table_name, "c", select_columns)
        where_sql = " OR ".join(f"({cond})" for cond in conds)
        rows = self.rows(
            f"""
            SELECT {select_list}, {time_expr} AS activity_time
            FROM {table_ref(self.schema, table_name)} c
            WHERE ({where_sql})
              {time_filter}
            ORDER BY activity_time DESC NULLS LAST, c.source_id DESC NULLS LAST
            LIMIT :limit_n
            """,
            params,
        )
        return [compact_dict(row) for row in rows]

    def collect_checkout_feedback(
        self,
        staff: dict[str, Any],
        properties: Sequence[dict[str, Any]],
        start_dt: datetime,
        end_dt: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        table_name = "staging_checkout_form"
        booking_table = "staging_booking_confirm"
        if not self.table_exists(table_name):
            return []
        columns = self.table_columns(table_name)
        refs = set(self.staff_refs(staff))
        prop_ids = {str(row.get("prop_id") or row.get("source_id")) for row in properties if row.get("prop_id") or row.get("source_id")}
        conds: list[str] = []
        params: dict[str, Any] = {"start_dt": start_dt, "end_dt": end_dt, "limit_n": int(limit)}
        join_sql = ""
        booking_prop_select = "NULL AS booking_prop_id"
        if self.table_exists(booking_table) and "booking_id" in columns and "prop_id" in self.table_columns(booking_table):
            join_sql = f"LEFT JOIN {table_ref(self.schema, booking_table)} bc ON bc.source_id::text = co.booking_id::text OR bc.booking_id::text = co.booking_id::text"
            booking_prop_select = "bc.prop_id AS booking_prop_id"
        ref_columns = [col for col in ("caretaker", "supervisor", "ops_manager", "salesperson") if col in columns]
        if refs and ref_columns:
            in_sql, in_params = build_in_params(sorted(refs), "coref")
            params.update(in_params)
            conds.extend([f"LOWER(TRIM(COALESCE(co.{_safe_ident(col)}::text, ''))) IN {in_sql}" for col in ref_columns])
        if prop_ids and join_sql:
            in_sql, in_params = build_in_params(sorted(prop_ids), "coprop")
            params.update(in_params)
            conds.append(f"bc.prop_id::text IN {in_sql}")
        if not conds:
            return []
        time_expr = self.coalesce_existing(table_name, "co", ["checkout_date", "added_time", "synced_at"])
        time_filter = f"AND {time_expr} >= :start_dt AND {time_expr} < :end_dt" if time_expr != "NULL" else ""
        select_columns = [
            "source_id", "booking_id", "user_email", "checkout_date", "rms_rating", "building_rating",
            "refer_friends_score", "rms_comment", "stay_comment", "suggestions", "other_comment",
            "supervisor", "caretaker", "ops_manager", "salesperson", "added_time", "synced_at",
        ]
        select_list = self.select_exprs(table_name, "co", select_columns)
        where_sql = " OR ".join(f"({cond})" for cond in conds)
        rows = self.rows(
            f"""
            SELECT {select_list}, {booking_prop_select}, {time_expr} AS activity_time
            FROM {table_ref(self.schema, table_name)} co
            {join_sql}
            WHERE ({where_sql})
              {time_filter}
            ORDER BY activity_time DESC NULLS LAST, co.source_id DESC NULLS LAST
            LIMIT :limit_n
            """,
            params,
        )
        return [compact_dict(row) for row in rows]

    def collect_site_visits(
        self,
        staff: dict[str, Any],
        buildings: Sequence[dict[str, Any]],
        properties: Sequence[dict[str, Any]],
        start_dt: datetime,
        end_dt: datetime,
        limit: int,
        *,
        include_scope: bool = False,
    ) -> list[dict[str, Any]]:
        table_name = "staging_site_visits"
        if not self.table_exists(table_name):
            return []   

        columns = self.table_columns(table_name)
        refs = set(self.staff_refs(staff))

        building_ids = {
            str(row.get("building_id") or row.get("source_id"))
            for row in buildings
            if row.get("building_id") or row.get("source_id")
        }

        params: dict[str, Any] = {
            "start_dt": start_dt,
            "end_dt": end_dt,
            "limit_n": int(limit),
        }

        time_expr = self.coalesce_existing(table_name, "sv", ["site_visit_date", "added_on", "synced_at"])
        time_filter = f"AND {time_expr} >= :start_dt AND {time_expr} < :end_dt" if time_expr != "NULL" else ""

        select_columns = [
            "source_id",
            "lead_id",
            "executive_id",
            "building_id",
            "prop_id",
            "unit_type",
            "schedule_status",
            "visit_type",
            "site_visit_date",
            "added_on",
            "synced_at",
        ]
        select_list = self.select_exprs(table_name, "sv", select_columns)

        # Caretaker / scoped role mapping:
        # staff -> assigned buildings -> staging_site_visits.building_id.
        # Do not use executive_id or prop_id for caretaker ownership.
        if include_scope:
            if not building_ids or "building_id" not in columns:
                return []

            building_in_sql, building_in_params = build_in_params(sorted(building_ids), "svbid")
            params.update(building_in_params)

            join_building_sql = ""
            building_select_sql = """
                NULL AS building_name,
                NULL AS building_caretaker,
            """

            if self.table_exists("staging_buildings"):
                join_building_sql = f"""
                LEFT JOIN {table_ref(self.schema, "staging_buildings")} b
                    ON b.building_id::text = sv.building_id::text
                """
                building_select_sql = """
                b.building_name AS building_name,
                b.caretaker AS building_caretaker,
                """

            join_user_sql = ""
            user_select_sql = """
                NULL AS caretaker_phone,
                NULL AS caretaker_normalized_phone,
            """

            if self.table_exists("staging_user_account"):
                join_user_sql = f"""
                LEFT JOIN {table_ref(self.schema, "staging_user_account")} ua
                    ON LOWER(TRIM(ua.username::text)) = LOWER(TRIM(b.caretaker::text))
                """
                user_select_sql = """
                ua.phone_number AS caretaker_phone,
                ua.normalized_phone AS caretaker_normalized_phone,
                """

            join_lead_sql = ""
            lead_select_sql = """
                NULL AS customer_phone,
                NULL AS customer_phone_alt,
                NULL AS lead_raw_status,
            """

            if self.table_exists("staging_lead_tracking"):
                join_lead_sql = f"""
                LEFT JOIN {table_ref(self.schema, "staging_lead_tracking")} lt
                    ON lt.source_id::text = sv.lead_id::text
                """
                lead_select_sql = """
                lt.contact_number AS customer_phone,
                lt.contact_number_alt AS customer_phone_alt,
                lt.raw_status AS lead_raw_status,
                """

            call_columns = (
                self.table_columns("staging_call_log_unified")
                if self.table_exists("staging_call_log_unified")
                else set()
            )
            call_ref_columns = [col for col in ("executive_id", "executive_name") if col in call_columns]
            pre_visit_actor_conds: list[str] = []
            if refs and call_ref_columns:
                in_sql, in_params = build_in_params(sorted(refs), "precallref")
                params.update(in_params)
                pre_visit_actor_conds.extend(
                    f"LOWER(TRIM(COALESCE(c.{_safe_ident(col)}::text, ''))) IN {in_sql}"
                    for col in call_ref_columns
                )

            actor_missing_sql = "TRUE"
            if call_ref_columns:
                actor_missing_sql = " AND ".join(
                    f"COALESCE(NULLIF(TRIM(c.{_safe_ident(col)}::text), ''), '') = ''"
                    for col in call_ref_columns
                )

            assigned_line_phone10s = self.staff_line_phone10s(staff)
            personal_phone10s = self.staff_phone10s(staff)
            if assigned_line_phone10s and "sales_phone" in call_columns:
                in_sql, in_params = build_in_params(assigned_line_phone10s, "precallline")
                params.update(in_params)
                pre_visit_actor_conds.append(
                    f"(({actor_missing_sql}) AND RIGHT(REGEXP_REPLACE(COALESCE(c.sales_phone::text, ''), '\\D', '', 'g'), 10) IN {in_sql})"
                )
            elif personal_phone10s and "sales_phone" in call_columns and not self.table_exists("staging_staff_phone_assignment"):
                in_sql, in_params = build_in_params(personal_phone10s, "precallphone")
                params.update(in_params)
                pre_visit_actor_conds.append(
                    f"(({actor_missing_sql}) AND RIGHT(REGEXP_REPLACE(COALESCE(c.sales_phone::text, ''), '\\D', '', 'g'), 10) IN {in_sql})"
                )

            pre_visit_actor_sql = (
                " OR ".join(f"({cond})" for cond in pre_visit_actor_conds)
                if pre_visit_actor_conds
                else "FALSE"
            )
            pre_visit_target_conds = ["c.lead_id::text = sv.lead_id::text"]
            if "counterparty_phone" in call_columns:
                customer_phone_sql = (
                    "RIGHT(REGEXP_REPLACE(COALESCE(sv.customer_phone::text, ''), '\\D', '', 'g'), 10)"
                )
                customer_alt_phone_sql = (
                    "RIGHT(REGEXP_REPLACE(COALESCE(sv.customer_phone_alt::text, ''), '\\D', '', 'g'), 10)"
                )
                call_counterparty_sql = (
                    "RIGHT(REGEXP_REPLACE(COALESCE(c.counterparty_phone::text, ''), '\\D', '', 'g'), 10)"
                )
                pre_visit_target_conds.append(
                    f"({call_counterparty_sql} <> '' AND {call_counterparty_sql} IN ({customer_phone_sql}, {customer_alt_phone_sql}))"
                )
            pre_visit_target_sql = " OR ".join(f"({cond})" for cond in pre_visit_target_conds)
            call_direction_sql = (
                "LOWER(COALESCE(c.call_direction::text, ''))"
                if "call_direction" in call_columns
                else "''"
            )

            rows = self.rows(
                f"""
                    WITH scope_visits AS (
                        SELECT
                        {select_list},
                        {time_expr} AS activity_time,
                        {building_select_sql}
                        {user_select_sql}
                        {lead_select_sql}
                        CASE
                            WHEN sv.schedule_status = 0 THEN 'done'
                            WHEN sv.schedule_status = 1 THEN 'scheduled_not_done'
                            ELSE 'unknown'
                        END AS visit_status,
                        'assigned_building' AS match_reason
                        FROM {table_ref(self.schema, table_name)} sv
                        {join_building_sql}
                        {join_user_sql}
                        {join_lead_sql}
                        WHERE sv.building_id::text IN {building_in_sql}
                        {time_filter}
                    ),
                    enriched AS (
                        SELECT
                            sv.*,

                            COALESCE(booking_check.overlapping_success_bookings, 0) AS overlapping_success_bookings,

                            COALESCE(call_check.calls_near_visit, 0) AS calls_near_visit,
                            COALESCE(call_check.connected_calls_near_visit, 0) AS connected_calls_near_visit,
                            COALESCE(call_check.missed_calls_near_visit, 0) AS missed_calls_near_visit,
                            COALESCE(call_check.missed_calls_from_caretaker_near_visit, 0) AS missed_calls_from_caretaker_near_visit,
                            COALESCE(call_check.missed_calls_from_customer_near_visit, 0) AS missed_calls_from_customer_near_visit,
                            COALESCE(call_check.missed_calls_unknown_direction_near_visit, 0) AS missed_calls_unknown_direction_near_visit,
                            COALESCE(call_check.non_zero_duration_calls_near_visit, 0) AS non_zero_duration_calls_near_visit,
                            COALESCE(call_check.connected_calls_after_last_missed, 0) AS connected_calls_after_last_missed,
                            call_check.first_call_near_visit,
                            call_check.last_call_near_visit,
                            call_check.last_missed_call_near_visit,
                            COALESCE(pre_visit_call_check.pre_visit_calls_same_day, 0) AS pre_visit_calls_same_day,
                            COALESCE(pre_visit_call_check.pre_visit_connected_calls_same_day, 0) AS pre_visit_connected_calls_same_day,
                            COALESCE(pre_visit_call_check.pre_visit_missed_calls_same_day, 0) AS pre_visit_missed_calls_same_day,
                            pre_visit_call_check.last_pre_visit_connected_call_time,
                            pre_visit_call_check.pre_visit_call_minutes_before,

                            CASE
                                WHEN sv.schedule_status = 0
                                    THEN 'DONE_VISIT'

                                WHEN COALESCE(call_check.missed_calls_near_visit, 0) > 0
                                AND COALESCE(call_check.connected_calls_after_last_missed, 0) = 0
                                    THEN 'NOT_DONE_MISSED_CALL_NO_CONNECTED_FOLLOWUP'

                                WHEN COALESCE(booking_check.overlapping_success_bookings, 0) > 0
                                    THEN 'NOT_DONE_PROPERTY_ALREADY_BOOKED_OR_FULL'

                                WHEN COALESCE(call_check.connected_calls_near_visit, 0) > 0
                                    THEN 'NOT_DONE_BUT_CONNECTED_FOLLOWUP_FOUND'

                                WHEN COALESCE(call_check.calls_near_visit, 0) = 0
                                    THEN 'NOT_DONE_NO_BOOKING_NO_CALL_ACTIVITY'

                                ELSE 'NOT_DONE_UNKNOWN_REASON'
                            END AS not_done_reason_candidate

                        FROM scope_visits sv

                        LEFT JOIN LATERAL (
                            SELECT
                                COUNT(*) AS overlapping_success_bookings
                            FROM {table_ref(self.schema, "staging_booking_confirm")} bc
                            WHERE sv.schedule_status = 1
                            AND bc.prop_id::text = sv.prop_id::text
                            AND LOWER(COALESCE(bc.booking_status, '')) = 'success'
                            AND sv.activity_time::date BETWEEN bc.travel_from_date AND bc.travel_to_date
                        ) booking_check ON TRUE

                        LEFT JOIN LATERAL (
                        SELECT
                            COUNT(*) AS calls_near_visit,

                            COUNT(*) FILTER (
                                WHERE LOWER(COALESCE(c.call_result, '')) = 'connected'
                                OR COALESCE(c.talk_time_sec, 0) > 0
                            ) AS connected_calls_near_visit,

                            COUNT(*) FILTER (
                                WHERE LOWER(COALESCE(c.call_result, '')) = 'missed'
                                OR COALESCE(c.talk_time_sec, 0) <= 0
                            ) AS missed_calls_near_visit,

                            COUNT(*) FILTER (
                                WHERE (
                                    LOWER(COALESCE(c.call_result, '')) = 'missed'
                                    OR COALESCE(c.talk_time_sec, 0) <= 0
                                )
                                AND {call_direction_sql} IN ('outgoing', 'outbound')
                            ) AS missed_calls_from_caretaker_near_visit,

                            COUNT(*) FILTER (
                                WHERE (
                                    LOWER(COALESCE(c.call_result, '')) = 'missed'
                                    OR COALESCE(c.talk_time_sec, 0) <= 0
                                )
                                AND {call_direction_sql} IN ('incoming', 'inbound')
                            ) AS missed_calls_from_customer_near_visit,

                            COUNT(*) FILTER (
                                WHERE (
                                    LOWER(COALESCE(c.call_result, '')) = 'missed'
                                    OR COALESCE(c.talk_time_sec, 0) <= 0
                                )
                                AND {call_direction_sql} NOT IN ('outgoing', 'outbound', 'incoming', 'inbound')
                            ) AS missed_calls_unknown_direction_near_visit,

                            COUNT(*) FILTER (
                                WHERE COALESCE(c.talk_time_sec, 0) > 0
                            ) AS non_zero_duration_calls_near_visit,

                            MIN(c.call_time) AS first_call_near_visit,
                            MAX(c.call_time) AS last_call_near_visit,

                            MAX(c.call_time) FILTER (
                                WHERE LOWER(COALESCE(c.call_result, '')) = 'missed'
                                OR COALESCE(c.talk_time_sec, 0) <= 0
                            ) AS last_missed_call_near_visit,

                            COUNT(*) FILTER (
                                WHERE (
                                    LOWER(COALESCE(c.call_result, '')) = 'connected'
                                    OR COALESCE(c.talk_time_sec, 0) > 0
                                )
                                AND c.call_time > (
                                    SELECT MAX(c2.call_time)
                                    FROM {table_ref(self.schema, "staging_call_log_unified")} c2
                                    WHERE c2.lead_id::text = sv.lead_id::text
                                    AND c2.call_time >= sv.activity_time - INTERVAL '24 hours'
                                    AND c2.call_time <= sv.activity_time + INTERVAL '12 hours'
                                    AND (
                                            LOWER(COALESCE(c2.call_result, '')) = 'missed'
                                            OR COALESCE(c2.talk_time_sec, 0) <= 0
                                    )
                                )
                            ) AS connected_calls_after_last_missed

                        FROM {table_ref(self.schema, "staging_call_log_unified")} c
                        WHERE sv.schedule_status = 1
                        AND c.lead_id::text = sv.lead_id::text
                        AND c.call_time >= sv.activity_time - INTERVAL '24 hours'
                        AND c.call_time <= sv.activity_time + INTERVAL '12 hours'
                    ) call_check ON TRUE

                        LEFT JOIN LATERAL (
                            SELECT
                                COUNT(*) AS pre_visit_calls_same_day,

                                COUNT(*) FILTER (
                                    WHERE LOWER(COALESCE(c.call_result, '')) = 'connected'
                                    OR COALESCE(c.talk_time_sec, 0) > 0
                                ) AS pre_visit_connected_calls_same_day,

                                COUNT(*) FILTER (
                                    WHERE LOWER(COALESCE(c.call_result, '')) = 'missed'
                                    OR COALESCE(c.talk_time_sec, 0) <= 0
                                ) AS pre_visit_missed_calls_same_day,

                                MAX(c.call_time) FILTER (
                                    WHERE LOWER(COALESCE(c.call_result, '')) = 'connected'
                                    OR COALESCE(c.talk_time_sec, 0) > 0
                                ) AS last_pre_visit_connected_call_time,

                                ROUND(
                                    (
                                        EXTRACT(
                                            EPOCH FROM (
                                                sv.activity_time
                                                - MAX(c.call_time) FILTER (
                                                    WHERE LOWER(COALESCE(c.call_result, '')) = 'connected'
                                                    OR COALESCE(c.talk_time_sec, 0) > 0
                                                )
                                            )
                                        ) / 60.0
                                    )::numeric,
                                    2
                                ) AS pre_visit_call_minutes_before

                            FROM {table_ref(self.schema, "staging_call_log_unified")} c
                            WHERE sv.schedule_status = 0
                            AND c.call_time::date = sv.activity_time::date
                            AND c.call_time <= sv.activity_time
                            AND ({pre_visit_target_sql})
                            AND ({pre_visit_actor_sql})
                        ) pre_visit_call_check ON TRUE
                    )
                    SELECT *
                    FROM enriched
                    ORDER BY activity_time DESC NULLS LAST, source_id DESC NULLS LAST
                    LIMIT :limit_n
                """,
                params,
            )
            return [compact_dict(row) for row in rows]

        # Non-scoped fallback: keep old direct executive_id matching.
        conds: list[str] = []

        if refs and "executive_id" in columns:
            in_sql, in_params = build_in_params(sorted(refs), "svref")
            params.update(in_params)
            conds.append(f"LOWER(TRIM(COALESCE(sv.executive_id::text, ''))) IN {in_sql}")

        if not conds:
            return []

        where_sql = " OR ".join(f"({cond})" for cond in conds)

        rows = self.rows(
            f"""
            SELECT {select_list}, {time_expr} AS activity_time
            FROM {table_ref(self.schema, table_name)} sv
            WHERE ({where_sql})
            {time_filter}
            ORDER BY activity_time DESC NULLS LAST, sv.source_id DESC NULLS LAST
            LIMIT :limit_n
            """,
            params,
        )

        return [compact_dict(row) for row in rows]


    def collect_leads(
        self,
        staff: dict[str, Any],
        role_scope: str,
        start_dt: datetime,
        end_dt: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        table_name = "staging_lead_tracking"
        if not self.table_exists(table_name):
            return []
        columns = self.table_columns(table_name)
        config = ROLE_CONFIG.get(role_scope) or ROLE_CONFIG["generic"]
        ref_columns = [col for col in config.get("lead_ref_columns", []) if col in columns]
        if not ref_columns:
            return []
        refs = set(self.staff_refs(staff))
        if not refs:
            return []

        in_sql, in_params = build_in_params(sorted(refs), "leadref")
        conds = [f"LOWER(TRIM(COALESCE(l.{_safe_ident(col)}::text, ''))) IN {in_sql}" for col in ref_columns]
        params: dict[str, Any] = {**in_params, "start_dt": start_dt, "end_dt": end_dt, "limit_n": int(limit)}
        time_expr = self.coalesce_existing(table_name, "l", ["created_at", "closed_at", "synced_at"])
        time_filter = f"AND {time_expr} >= :start_dt AND {time_expr} < :end_dt" if time_expr != "NULL" else ""
        select_columns = [
            "source_id", "user_id", "booking_id", "executive_id", "created_at", "closed_at",
            "raw_status", "email", "priority", "added_by", "assigned_to", "generated_by",
            "origin", "contact_number", "contact_number_alt", "synced_at",
        ]
        select_list = self.select_exprs(table_name, "l", select_columns)
        where_sql = " OR ".join(f"({cond})" for cond in conds)
        rows = self.rows(
            f"""
            SELECT {select_list}, {time_expr} AS activity_time
            FROM {table_ref(self.schema, table_name)} l
            WHERE ({where_sql})
              {time_filter}
            ORDER BY activity_time DESC NULLS LAST, l.source_id DESC NULLS LAST
            LIMIT :limit_n
            """,
            params,
        )

        out: list[dict[str, Any]] = []
        for row in rows:
            match_columns = [col for col in ref_columns if lower_ref(row.get(col)) in refs]
            row["match_reasons"] = [f"{col}_staff" for col in match_columns]
            row["customer_phone"] = show_phone(row.get("contact_number") or row.get("contact_number_alt"))
            out.append(compact_dict(row))
        return out

    def collect_bookings(
        self,
        staff: dict[str, Any],
        leads: Sequence[dict[str, Any]],
        role_scope: str,
        start_dt: datetime,
        end_dt: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        table_name = "staging_booking_confirm"
        if not self.table_exists(table_name):
            return []
        columns = self.table_columns(table_name)
        refs = set(self.staff_refs(staff))
        lead_ids = {str(row.get("source_id")) for row in leads if row.get("source_id") not in (None, "")}
        config = ROLE_CONFIG.get(role_scope) or ROLE_CONFIG["generic"]
        ref_columns = [col for col in config.get("booking_ref_columns", []) if col in columns]

        conds: list[str] = []
        params: dict[str, Any] = {"start_dt": start_dt, "end_dt": end_dt, "limit_n": int(limit)}
        if refs and ref_columns:
            in_sql, in_params = build_in_params(sorted(refs), "bookref")
            params.update(in_params)
            conds.extend([f"LOWER(TRIM(COALESCE(bc.{_safe_ident(col)}::text, ''))) IN {in_sql}" for col in ref_columns])
        if lead_ids and "lead_id" in columns:
            in_sql, in_params = build_in_params(sorted(lead_ids), "booklead")
            params.update(in_params)
            conds.append(f"bc.lead_id::text IN {in_sql}")
        if not conds:
            return []

        time_expr = self.coalesce_existing(table_name, "bc", ["booking_datetime", "travel_from_date", "synced_at"])
        time_filter = f"AND {time_expr} >= :start_dt AND {time_expr} < :end_dt" if time_expr != "NULL" else ""
        select_columns = [
            "source_id", "booking_id", "user_id", "lead_id", "prop_id", "booking_status",
            "booking_type", "booking_source", "txn_source", "travel_from_date", "travel_to_date",
            "total_amount", "early_cout", "before_disc_monthly", "after_disc_month_rent",
            "num_guests", "rent_margin", "booking_datetime", "created_by", "synced_at",
        ]
        select_list = self.select_exprs(table_name, "bc", select_columns)
        where_sql = " OR ".join(f"({cond})" for cond in conds)
        rows = self.rows(
            f"""
            SELECT {select_list}, {time_expr} AS activity_time
            FROM {table_ref(self.schema, table_name)} bc
            WHERE ({where_sql})
              {time_filter}
            ORDER BY activity_time DESC NULLS LAST, bc.source_id DESC NULLS LAST
            LIMIT :limit_n
            """,
            params,
        )
        return [compact_dict(row) for row in rows]

    def collect_travel_cart_for_leads(
        self,
        leads: Sequence[dict[str, Any]],
        start_dt: datetime,
        end_dt: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        table_name = "staging_travel_cart"
        if not leads or not self.table_exists(table_name):
            return []
        columns = self.table_columns(table_name)
        user_ids = {str(row.get("user_id")) for row in leads if row.get("user_id") not in (None, "")}
        if not user_ids or "user_id" not in columns:
            return []
        in_sql, in_params = build_in_params(sorted(user_ids), "tcuser")
        params: dict[str, Any] = {**in_params, "start_dt": start_dt, "end_dt": end_dt, "limit_n": int(limit)}
        time_expr = self.coalesce_existing(table_name, "tc", ["added_on", "synced_at"])
        time_filter = f"AND {time_expr} >= :start_dt AND {time_expr} < :end_dt" if time_expr != "NULL" else ""
        select_columns = [
            "source_id", "user_id", "prop_id", "travel_from_date", "travel_to_date", "nights",
            "booking_type", "total_amount", "advance_amount", "pending_amount", "source", "added_on",
            "bkc_status", "synced_at",
        ]
        select_list = self.select_exprs(table_name, "tc", select_columns)
        rows = self.rows(
            f"""
            SELECT {select_list}, {time_expr} AS activity_time
            FROM {table_ref(self.schema, table_name)} tc
            WHERE tc.user_id::text IN {in_sql}
              {time_filter}
            ORDER BY activity_time DESC NULLS LAST, tc.source_id DESC NULLS LAST
            LIMIT :limit_n
            """,
            params,
        )
        return [compact_dict(row) for row in rows]

    def collect_finance_rows(self, staff: dict[str, Any], start_dt: datetime, end_dt: datetime, limit: int) -> list[dict[str, Any]]:
        table_name = "staging_booking_invoice_details"
        if not self.table_exists(table_name):
            return []
        columns = self.table_columns(table_name)
        refs = set(self.staff_refs(staff))
        ref_columns = [col for col in ("utr_added_by", "created_by", "updated_by", "added_by") if col in columns]
        if not refs or not ref_columns:
            return []
        in_sql, in_params = build_in_params(sorted(refs), "finref")
        conds = [f"LOWER(TRIM(COALESCE(inv.{_safe_ident(col)}::text, ''))) IN {in_sql}" for col in ref_columns]
        params: dict[str, Any] = {**in_params, "start_dt": start_dt, "end_dt": end_dt, "limit_n": int(limit)}
        time_expr = self.coalesce_existing(table_name, "inv", ["utr_added_on", "created_on", "send_time", "synced_at"])
        time_filter = f"AND {time_expr} >= :start_dt AND {time_expr} < :end_dt" if time_expr != "NULL" else ""
        select_columns = [
            "source_id", "booking_id", "payment_id", "amount_status", "duration_period", "mail_status",
            "sa_mail_status", "reminder_mail", "amount_recieved", "amount", "total_amount", "disc",
            "from_date", "till_date", "pending_balance", "payment_mode", "comment", "status",
            "mail_count", "send_time", "modify_flag", "transaction_type", "created_on", "utr_no",
            "utr_added_by", "om_rent", "sa_rent", "rent_receipt_dw", "sa_receipt_dw", "utr_added_on", "synced_at",
        ]
        select_list = self.select_exprs(table_name, "inv", select_columns)
        where_sql = " OR ".join(f"({cond})" for cond in conds)
        rows = self.rows(
            f"""
            SELECT {select_list}, {time_expr} AS activity_time
            FROM {table_ref(self.schema, table_name)} inv
            WHERE ({where_sql})
              {time_filter}
            ORDER BY activity_time DESC NULLS LAST, inv.source_id DESC NULLS LAST
            LIMIT :limit_n
            """,
            params,
        )
        return [compact_dict(row) for row in rows]

    def collect_property_marks_by_staff(
        self,
        staff: dict[str, Any],
        start_dt: datetime,
        end_dt: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        table_name = "staging_property_unit"
        building_table = "staging_buildings"
        if not self.table_exists(table_name):
            return []
        columns = self.table_columns(table_name)
        refs = set(self.staff_refs(staff))
        ref_columns = [col for col in ("last_updated_by", "asset_verified_by", "flat_verified_by", "prop_info_last_updated_by") if col in columns]
        if not refs or not ref_columns:
            return []
        in_sql, in_params = build_in_params(sorted(refs), "pmref")
        conds = [f"LOWER(TRIM(COALESCE(p.{_safe_ident(col)}::text, ''))) IN {in_sql}" for col in ref_columns]
        params: dict[str, Any] = {**in_params, "start_dt": start_dt, "end_dt": end_dt, "limit_n": int(limit)}
        time_expr = self.coalesce_existing(
            table_name,
            "p",
            ["prop_info_last_update_time", "last_updated_on", "asset_verified_on", "flat_verified_on", "synced_at"],
        )
        time_filter = f"AND {time_expr} >= :start_dt AND {time_expr} < :end_dt" if time_expr != "NULL" else ""
        p_columns = [
            "source_id", "prop_id", "building_id", "unit_name", "unit_number", "unit_type", "rms_prop",
            "bookable", "active", "verified", "available_from_date", "mark_check_out",
            "mark_electricity_bill", "mark_rent_paid", "asset_verified", "asset_verified_by",
            "asset_verified_on", "flat_verified", "flat_verified_by", "flat_verified_on",
            "last_updated_by", "last_updated_on", "prop_info_last_update_time", "prop_info_last_updated_by",
            "synced_at",
        ]
        b_columns = ["building_name", "city", "area"]
        select_list = self.select_exprs(table_name, "p", p_columns)
        if self.table_exists(building_table) and "building_id" in columns and "building_id" in self.table_columns(building_table):
            select_list += ",\n               " + self.select_exprs(building_table, "b", b_columns, prefix="building_")
            join_sql = f"LEFT JOIN {table_ref(self.schema, building_table)} b ON b.building_id::text = p.building_id::text"
        else:
            select_list += ",\n               " + ",\n               ".join(f"NULL AS building_{_safe_ident(c)}" for c in b_columns)
            join_sql = ""
        where_sql = " OR ".join(f"({cond})" for cond in conds)
        rows = self.rows(
            f"""
            SELECT {select_list}, {time_expr} AS activity_time
            FROM {table_ref(self.schema, table_name)} p
            {join_sql}
            WHERE ({where_sql})
              {time_filter}
            ORDER BY activity_time DESC NULLS LAST, p.source_id DESC NULLS LAST
            LIMIT :limit_n
            """,
            params,
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            row["match_reasons"] = [f"{col}_staff" for col in ref_columns if lower_ref(row.get(col)) in refs]
            out.append(compact_dict(row))
        return out

    def collect_calls(self, staff: dict[str, Any], start_dt: datetime, end_dt: datetime, limit: int, max_text: int) -> list[dict[str, Any]]:
        table_name = "staging_call_log_unified" if self.table_exists("staging_call_log_unified") else "staging_call_recordings_transcript"
        if not self.table_exists(table_name):
            return []
        columns = self.table_columns(table_name)
        refs = set(self.staff_refs(staff))
        assigned_line_phone10s = self.staff_line_phone10s(staff)
        personal_phone10s = self.staff_phone10s(staff)
        conds: list[str] = []
        params: dict[str, Any] = {"start_dt": start_dt, "end_dt": end_dt, "limit_n": int(limit)}

        ref_columns = [c for c in ("executive_id", "executive_name") if c in columns]
        if refs and ref_columns:
            in_sql, in_params = build_in_params(sorted(refs), "cref")
            params.update(in_params)
            conds.extend([f"LOWER(TRIM(COALESCE(c.{_safe_ident(col)}::text, ''))) IN {in_sql}" for col in ref_columns])

        # Pooled line fallback: only use sales_phone -> MobileTagging when the
        # call log actor is missing. If executive_id/name exists, it is the actor.
        actor_missing_sql = "TRUE"
        if ref_columns:
            actor_missing_sql = " AND ".join(
                f"COALESCE(NULLIF(TRIM(c.{_safe_ident(col)}::text), ''), '') = ''" for col in ref_columns
            )
        if assigned_line_phone10s and "sales_phone" in columns:
            in_sql, in_params = build_in_params(assigned_line_phone10s, "cline")
            params.update(in_params)
            conds.append(
                f"(({actor_missing_sql}) AND RIGHT(REGEXP_REPLACE(COALESCE(c.sales_phone::text, ''), '\\D', '', 'g'), 10) IN {in_sql})"
            )
        elif personal_phone10s and "sales_phone" in columns and not self.table_exists("staging_staff_phone_assignment"):
            # Legacy fallback only for older deployments without MobileTagging.
            in_sql, in_params = build_in_params(personal_phone10s, "cphone")
            params.update(in_params)
            conds.append(
                f"(({actor_missing_sql}) AND RIGHT(REGEXP_REPLACE(COALESCE(c.sales_phone::text, ''), '\\D', '', 'g'), 10) IN {in_sql})"
            )

        if not conds or "call_time" not in columns:
            return []
        select_columns = [
            "source_id", "lead_id", "executive_id", "executive_name", "call_time", "talk_time_sec",
            "call_direction", "call_result", "counterparty_phone", "sales_phone", "department", "audio_url",
            "translated_text", "transcript_text", "transcript_text_eleven_labs", "raw_transcripts",
            "intent", "emotion", "tone", "action_layer", "context", "outcome", "language", "source_call_id",
            "filename", "uploaded_at", "synced_at",
        ]
        select_list = self.select_exprs(table_name, "c", select_columns)
        where_sql = " OR ".join(f"({cond})" for cond in conds)
        rows = self.rows(
            f"""
            SELECT {select_list}
            FROM {table_ref(self.schema, table_name)} c
            WHERE ({where_sql})
              AND c.call_time >= :start_dt
              AND c.call_time < :end_dt
            ORDER BY c.call_time DESC NULLS LAST, c.source_id DESC NULLS LAST
            LIMIT :limit_n
            """,
            params,
        )
        out: list[dict[str, Any]] = []
        ref_set = set(refs)
        assigned_line_set = set(assigned_line_phone10s)
        for row in rows:
            duration = int(row.get("talk_time_sec") or 0)
            transcript = clean_text(
                row.get("translated_text") or row.get("transcript_text") or row.get("transcript_text_eleven_labs") or row.get("raw_transcripts"),
                max_text,
            )
            parties = self._call_parties(row)
            caller = parties.get("caller") or {}
            receiver = parties.get("receiver") or {}
            phone_line = parties.get("phone_line") or {}
            attribution = parties.get("attribution") or "unknown_actor"

            match_reasons: list[str] = []
            for col in ref_columns:
                if lower_ref(row.get(col)) in ref_set:
                    match_reasons.append(f"{col}_staff")
            sales_phone10 = phone_last10(row.get("sales_phone"))
            actor_missing = not any(clean_text(row.get(col), 100) for col in ref_columns)
            if actor_missing and sales_phone10 in assigned_line_set:
                match_reasons.append("fallback_from_line_assignment")
            elif actor_missing and sales_phone10 in personal_phone10s:
                match_reasons.append("legacy_sales_phone_match")

            summary_bits = [
                f"{self._party_label(caller)} -> {self._party_label(receiver)}",
                str(row.get("call_result") or "").strip(),
                f"{duration}s",
            ]
            if phone_line.get("number"):
                pooled = phone_line.get("pooled_username") or phone_line.get("pooled_tag_to")
                if pooled:
                    summary_bits.append(f"line={phone_line.get('number')} pooled:{pooled}/{phone_line.get('pooled_team') or ''}".rstrip("/"))
                else:
                    summary_bits.append(f"line={phone_line.get('number')}")
            if attribution == "fallback_from_line_assignment":
                summary_bits.append("attribution=fallback_from_line_assignment")
            outcome = clean_text(row.get("outcome") or row.get("intent") or transcript, max_text)
            if outcome:
                summary_bits.append(outcome)

            out_row = compact_dict(
                {
                    "time": row.get("call_time"),
                    "channel": "call",
                    "source_table": table_name,
                    "source_id": row.get("source_id"),
                    "lead_id": row.get("lead_id"),
                    "flow": parties.get("flow"),
                    "call_type": parties.get("call_type"),
                    "from": caller,
                    "to": receiver,
                    "direction": row.get("call_direction"),
                    "direction_known": parties.get("direction_known"),
                    "status": row.get("call_result"),
                    "duration_sec": duration,
                    "executive": row.get("executive_id"),
                    "executive_name": row.get("executive_name"),
                    "office_number": show_phone(row.get("sales_phone")),
                    "counterparty_number": show_phone(row.get("counterparty_phone")),
                    "phone_line": phone_line,
                    "line_assignment": parties.get("line_assignment"),
                    "attribution": attribution,
                    "match_reasons": match_reasons,
                    "summary": clean_text(" | ".join(bit for bit in summary_bits if bit), max_text),
                    "transcript": transcript,
                    "audio_url": row.get("audio_url"),
                }
            )
            out_row["line"] = self._call_line(out_row)
            out.append(compact_dict(out_row))
        return out

    def collect_whatsapp(self, staff: dict[str, Any], start_dt: datetime, end_dt: datetime, limit: int, max_text: int) -> list[dict[str, Any]]:
        table_name = "staging_whatsapp_messages"
        if not self.table_exists(table_name):
            return []
        columns = self.table_columns(table_name)
        refs = set(self.staff_refs(staff))
        assigned_line_phone10s = self.staff_line_phone10s(staff)
        personal_phone10s = self.staff_phone10s(staff)
        conds: list[str] = []
        params: dict[str, Any] = {"start_dt": start_dt, "end_dt": end_dt, "limit_n": int(limit)}

        if refs and "executive_id" in columns:
            in_sql, in_params = build_in_params(sorted(refs), "wref")
            params.update(in_params)
            conds.append(f"LOWER(TRIM(COALESCE(w.executive_id::text, ''))) IN {in_sql}")

        actor_missing_sql = "COALESCE(NULLIF(TRIM(w.executive_id::text), ''), '') = ''" if "executive_id" in columns else "TRUE"
        if assigned_line_phone10s and "admin_number" in columns:
            in_sql, in_params = build_in_params(assigned_line_phone10s, "wline")
            params.update(in_params)
            conds.append(
                f"(({actor_missing_sql}) AND RIGHT(REGEXP_REPLACE(COALESCE(w.admin_number::text, ''), '\\D', '', 'g'), 10) IN {in_sql})"
            )
        elif personal_phone10s and "admin_number" in columns and not self.table_exists("staging_staff_phone_assignment"):
            in_sql, in_params = build_in_params(personal_phone10s, "wphone")
            params.update(in_params)
            conds.append(
                f"(({actor_missing_sql}) AND RIGHT(REGEXP_REPLACE(COALESCE(w.admin_number::text, ''), '\\D', '', 'g'), 10) IN {in_sql})"
            )

        if not conds or "message_time" not in columns:
            return []
        select_columns = [
            "source_id", "lead_id", "message_time", "direction", "executive_id", "message_type",
            "clean_content", "isread", "issent", "admin_number", "cx_number", "remote_jid", "synced_at",
        ]
        select_list = self.select_exprs(table_name, "w", select_columns)
        where_sql = " OR ".join(f"({cond})" for cond in conds)
        rows = self.rows(
            f"""
            SELECT {select_list}
            FROM {table_ref(self.schema, table_name)} w
            WHERE ({where_sql})
              AND w.message_time >= :start_dt
              AND w.message_time < :end_dt
            ORDER BY w.message_time DESC NULLS LAST, w.source_id DESC NULLS LAST
            LIMIT :limit_n
            """,
            params,
        )
        out: list[dict[str, Any]] = []
        ref_set = set(refs)
        assigned_line_set = set(assigned_line_phone10s)
        for row in rows:
            remote_jid = str(row.get("remote_jid") or "").strip()
            conversation_kind = "group" if remote_jid.lower().endswith("@g.us") else "direct" if remote_jid else "unknown"
            message_text = clean_text(row.get("clean_content"), max_text)
            if not message_text:
                mt = clean_text(row.get("message_type"), 80)
                message_text = f"[{mt}]" if mt else "[message]"
            phone_line = self._line_identity(row.get("admin_number"))
            executive_id = clean_text(row.get("executive_id"), 100)
            admin_phone10 = phone_last10(row.get("admin_number"))
            actor_missing = not executive_id
            attribution = "whatsapp_executive" if executive_id else "unknown_actor"
            match_reasons: list[str] = []
            if lower_ref(executive_id) in ref_set:
                match_reasons.append("executive_id_staff")
            if actor_missing and admin_phone10 in assigned_line_set:
                attribution = "fallback_from_line_assignment"
                match_reasons.append("fallback_from_line_assignment")
            elif actor_missing and admin_phone10 in personal_phone10s:
                attribution = "legacy_admin_number_match"
                match_reasons.append("legacy_admin_number_match")

            out_row = compact_dict(
                {
                    "time": row.get("message_time"),
                    "channel": "whatsapp",
                    "conversation_kind": conversation_kind,
                    "source_id": row.get("source_id"),
                    "lead_id": row.get("lead_id"),
                    "flow": direction_flow(row.get("direction"), staff_role="staff"),
                    "direction": row.get("direction"),
                    "status": "read" if str(row.get("isread")).lower() in {"1", "true", "t", "yes"} else "sent" if str(row.get("issent")).lower() in {"1", "true", "t", "yes"} else "",
                    "executive": row.get("executive_id"),
                    "staff_number": show_phone(row.get("admin_number")),
                    "counterparty_number": show_phone(row.get("cx_number")),
                    "office_number": show_phone(row.get("admin_number")),
                    "phone_line": phone_line,
                    "line_assignment": phone_line.get("line_assignment") if isinstance(phone_line, dict) else None,
                    "attribution": attribution,
                    "match_reasons": match_reasons,
                    "remote_jid": remote_jid,
                    "message_type": row.get("message_type"),
                    "text": message_text,
                }
            )
            out.append(out_row)
        return out

    # ------------------------------------------------------------------
    # Assembly / views
    # ------------------------------------------------------------------
    def _office_numbers(self, calls: Sequence[dict[str, Any]], whatsapp: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for source_name, rows in (("call", calls), ("whatsapp", whatsapp)):
            for row in rows:
                number = show_phone(row.get("office_number") or row.get("staff_number") or (row.get("phone_line") or {}).get("number"))
                if not number:
                    continue
                item = seen.setdefault(number, {"number": number, "sources": set(), "events": 0})
                item["sources"].add(source_name)
                item["events"] += 1
                line = row.get("phone_line") if isinstance(row.get("phone_line"), dict) else {}
                assignment = row.get("line_assignment") if isinstance(row.get("line_assignment"), dict) else line.get("line_assignment")
                if assignment:
                    item["pooled_username"] = assignment.get("username") or assignment.get("tag_to")
                    item["pooled_team"] = assignment.get("team")
                    item["line_type"] = "pooled"
        return [
            compact_dict(
                {
                    "number": number,
                    "line_type": value.get("line_type"),
                    "pooled": f"{value.get('pooled_username')}/{value.get('pooled_team')}" if value.get("pooled_username") or value.get("pooled_team") else None,
                    "sources": sorted(value["sources"]),
                    "events": value["events"],
                }
            )
            for number, value in sorted(seen.items())
        ]

    def _public_staff(self, staff: dict[str, Any]) -> dict[str, Any]:
        """Safe staff profile for evidence/LLM views. Keep source ids only in raw mode."""
        return compact_dict(
            {
                "username": staff.get("username"),
                "email": staff.get("email"),
                "phone": staff.get("normalized_phone") or show_phone(staff.get("phone_number")),
                "admin_role": staff_admin_role_display(staff),
                "team": staff.get("team"),
                "account_type": account_type_display(staff.get("is_admin")),
                "is_admin": staff_is_admin_display(staff),
                "active": staff.get("active"),
            }
        )

    @staticmethod
    def _is_system_actor(value: Any) -> bool:
        return lower_ref(value) in {"system", "auto", "automation", "scheduler"}

    def _site_visit_activity_type(self, row: dict[str, Any]) -> str:
        try:
            status_int = int(row.get("schedule_status"))
        except Exception:
            status_int = None

        if status_int == 0:
            return "actual_done_visit"

        if status_int == 1:
            reason = str(row.get("not_done_reason_candidate") or "").strip()
            return reason or "scheduled_not_done_visit"

        return "unknown_visit_status"

    def _site_visit_status_label(self, status: Any) -> str:
        try:
            status_int = int(status)
        except Exception:
            status_int = None

        if status_int == 0:
            return "done"
        if status_int == 1:
            return "scheduled_not_done"

        return "unknown"

    def _site_visit_counts(self, site_visits: Sequence[dict[str, Any]]) -> tuple[int, int]:
        """
        Returns:
            actual_done_visits, scheduled_not_done_visits

        Business rule:
        - schedule_status = 0 means actual/done visit
        - schedule_status = 1 means scheduled/not done
        - NULL/other statuses are counted as scheduled/not-done.
        """
        actual_done_visits = 0
        scheduled_not_done_visits = 0

        for row in site_visits or []:
            try:
                status = int(row.get("schedule_status"))
            except Exception:
                status = None

            if status == 0:
                actual_done_visits += 1
            else:
                scheduled_not_done_visits += 1
            
        return actual_done_visits, scheduled_not_done_visits

    def _counterparty_for_call(self, row: dict[str, Any]) -> dict[str, Any]:
        from_party = row.get("from") or {}
        to_party = row.get("to") or {}
        flow = str(row.get("flow") or "").strip()
        if flow == "staff_to_counterparty":
            return to_party
        if flow == "counterparty_to_staff":
            return from_party
        if (from_party or {}).get("role") == "counterparty":
            return from_party
        if (to_party or {}).get("role") == "counterparty":
            return to_party
        return to_party or from_party

    def _call_summary_by_counterparty(self, calls: Sequence[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in calls:
            party = self._counterparty_for_call(row)
            number = show_phone((party or {}).get("number")) or "unknown"
            label = self._party_label(party) if party else number
            item = grouped.setdefault(
                number,
                {
                    "counterparty": label,
                    "number": None if number == "unknown" else number,
                    "type": (party or {}).get("type") or row.get("call_type") or "unknown",
                    "calls": 0,
                    "connected": 0,
                    "missed": 0,
                    "talk_time_sec": 0,
                    "lead_ids": set(),
                    "last_time": None,
                },
            )
            item["calls"] += 1
            duration = int(row.get("duration_sec") or 0)
            if duration > 0:
                item["connected"] += 1
                item["talk_time_sec"] += duration
            else:
                item["missed"] += 1
            if row.get("lead_id") not in (None, ""):
                item["lead_ids"].add(row.get("lead_id"))
            row_time = coerce_datetime(row.get("time"))
            if row_time and (item["last_time"] is None or row_time > item["last_time"]):
                item["last_time"] = row_time

        out = []
        for item in grouped.values():
            out.append(
                compact_dict(
                    {
                        "counterparty": item.get("counterparty"),
                        "number": item.get("number"),
                        "type": item.get("type"),
                        "calls": item.get("calls"),
                        "connected": item.get("connected"),
                        "missed": item.get("missed"),
                        "talk_time": fmt_duration(item.get("talk_time_sec")),
                        "talk_time_sec": item.get("talk_time_sec"),
                        "lead_ids": sorted(item.get("lead_ids") or []),
                        "last_time": item.get("last_time"),
                    }
                )
            )
        out.sort(key=lambda r: (int(r.get("calls") or 0), coerce_datetime(r.get("last_time")) or datetime.min), reverse=True)
        return out[: max(1, int(limit or 12))]

    def _call_relation_label(self, row: dict[str, Any]) -> str:
        from_party = row.get("from") or {}
        to_party = row.get("to") or {}
        if not from_party and not to_party:
            return ""
        direction_known = row.get("direction_known")
        flow_text = str(row.get("flow") or "")
        unknown_direction = direction_known is False or "direction_unknown" in flow_text
        arrow = "↔" if unknown_direction else "->"
        return f"{self._party_label(from_party)} {arrow} {self._party_label(to_party)}"

    def _line_display_text(self, row: dict[str, Any]) -> Optional[str]:
        line = row.get("phone_line") if isinstance(row.get("phone_line"), dict) else {}
        assignment = row.get("line_assignment") if isinstance(row.get("line_assignment"), dict) else line.get("line_assignment")
        number = show_phone(row.get("office_number") or row.get("staff_number") or line.get("number"))
        if not number:
            return None
        text_value = f"line={number}"
        if assignment:
            pooled_user = assignment.get("username") or assignment.get("tag_to")
            pooled_team = assignment.get("team")
            pooled = "/".join(str(v) for v in (pooled_user, pooled_team) if v not in (None, ""))
            if pooled:
                text_value += f" pooled:{pooled}"
        return text_value

    def _call_line(self, row: dict[str, Any]) -> str:
        relation = self._call_relation_label(row)
        parts = [
            fmt_dt(row.get("time")),
            "call",
            str(row.get("flow") or "unknown"),
            str(row.get("call_type") or "unknown"),
        ]
        if relation:
            parts.append(relation)
        parts.extend(
            [
                str(row.get("status") or "unknown"),
                fmt_duration(row.get("duration_sec")),
            ]
        )
        line_text = self._line_display_text(row)
        if line_text:
            parts.append(line_text)
        if row.get("attribution") == "fallback_from_line_assignment":
            parts.append("attribution=fallback_from_line_assignment")
        if row.get("lead_id") not in (None, ""):
            parts.append(f"lead={row.get('lead_id')}")
        return " | ".join(part for part in parts if part)

    def _whatsapp_line(self, row: dict[str, Any]) -> str:
        parts = [
            fmt_dt(row.get("time")),
            "whatsapp",
            str(row.get("flow") or "unknown"),
            str(row.get("conversation_kind") or "unknown"),
        ]
        status = str(row.get("status") or "").strip()
        if status:
            parts.append(status)
        line_text = self._line_display_text(row)
        if line_text:
            parts.append(line_text)
        if row.get("attribution") == "fallback_from_line_assignment":
            parts.append("attribution=fallback_from_line_assignment")
        if row.get("lead_id") not in (None, ""):
            parts.append(f"lead={row.get('lead_id')}")
        text_value = clean_text(row.get("text"), 120)
        if text_value:
            parts.append(text_value)
        return " | ".join(part for part in parts if part)

    def _timeline_line(self, row: dict[str, Any]) -> str:
        if row.get("line"):
            return str(row.get("line"))
        if row.get("channel") == "call":
            return self._call_line(row)
        if row.get("channel") == "whatsapp":
            return self._whatsapp_line(row)
        parts = [fmt_dt(row.get("time")), str(row.get("channel") or ""), str(row.get("flow") or "")]
        status = str(row.get("status") or "").strip()
        if status:
            parts.append(status)
        text_value = str(row.get("text") or row.get("summary") or "").strip()
        if text_value:
            parts.append(text_value)
        return " | ".join(part for part in parts if part)

    def _compact_building_rows(self, buildings: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        rows = []
        for row in list(buildings)[:limit]:
            rows.append(
                compact_dict(
                    {
                        "BuildName": row.get("building_name"),
                        "BuildingId": row.get("building_id") or row.get("source_id"),
                    }
                )
            )
        return rows

    def _is_currently_vacant_bookable(self, row: dict[str, Any]) -> bool:
        """UI definition of vacant: not occupied, active, bookable, and available today.

        This intentionally excludes future-available units and units that are not bookable,
        even if no current booking row was found.
        """
        if row.get("occupancy_status") in {"occupied", "upcoming_booking", "unknown"}:
            return False
        if boolish(row.get("active")) is False:
            return False
        if boolish(row.get("bookable")) is not True:
            return False
        available_from = coerce_date(row.get("available_from_date"))
        return available_from is None or available_from <= now_ist_naive().date()

    def _vacant_properties(self, properties: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in properties if self._is_currently_vacant_bookable(row)]

    def _availability_inventory_properties(self, properties: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Assigned units not currently vacant/bookable, tracked as inventory context.

        These rows should not be treated as caretaker/Ops quality failures by default.
        They are availability-date/current-occupancy inventory unless the stricter
        quality follow-up rule below is met.
        """
        return [
            row for row in properties
            if row.get("occupancy_status") != "occupied" and not self._is_currently_vacant_bookable(row)
        ]

    def _availability_date_passed(self, row: dict[str, Any]) -> bool:
        available_from = coerce_date(row.get("available_from_date") or row.get("avl_date"))
        return bool(available_from and available_from <= now_ist_naive().date())

    def _checkout_completed_for_availability(self, row: dict[str, Any]) -> bool:
        booking = row.get("current_booking") if isinstance(row.get("current_booking"), dict) else {}
        for value in (
            row.get("mark_check_out"),
            row.get("check_out"),
            row.get("checkout_completed"),
            booking.get("has_checkout_evidence"),
        ):
            if boolish(value) is True:
                return True
            if coerce_date(value) or coerce_datetime(value):
                return True
            text_value = str(value or "").strip().lower()
            if text_value in {"completed", "complete", "done", "checked_out", "checked out", "checkout_done"}:
                return True
        return False

    def _is_availability_quality_followup(self, row: dict[str, Any]) -> bool:
        """Count against quality only when all required availability conditions are true."""
        return (
            row.get("occupancy_status") != "occupied"
            and self._availability_date_passed(row)
            and self._checkout_completed_for_availability(row)
            and boolish(row.get("bookable")) is not True
        )

    def _availability_quality_followup_properties(self, properties: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in self._availability_inventory_properties(properties) if self._is_availability_quality_followup(row)]

    def _availability_inventory_reason(self, row: dict[str, Any]) -> str:
        if row.get("occupancy_status") == "upcoming_booking":
            return "upcoming_booking"
        available_from = coerce_date(row.get("available_from_date") or row.get("avl_date"))
        if available_from and available_from > now_ist_naive().date():
            return "future_availability_date"
        if boolish(row.get("active")) is False:
            return "inactive"
        if boolish(row.get("bookable")) is not True:
            return "not_marked_bookable"
        if row.get("occupancy_status") in {"unknown", "vacant_non_success_booking"}:
            return str(row.get("occupancy_status"))
        if available_from is None:
            return "availability_date_missing"
        return "not_currently_available"

    def _compact_availability_inventory_rows(self, properties: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        rows = []
        for row in self._availability_inventory_properties(properties)[:limit]:
            quality_followup = self._is_availability_quality_followup(row)
            booking = row.get("current_booking") if isinstance(row.get("current_booking"), dict) else {}
            rows.append(
                compact_dict(
                    {
                        "property": property_unit_display(row),
                        "building": row.get("building_building_name"),
                        "area": row.get("building_area"),
                        "type": row.get("unit_type"),
                        "inventory_reason": self._availability_inventory_reason(row),
                        "occupancy_status": row.get("occupancy_status"),
                        "available_from": row.get("available_from_date") or row.get("avl_date"),
                        "checkout_completed": self._checkout_completed_for_availability(row),
                        "bookable": row.get("bookable"),
                        "active": row.get("active"),
                        "booking_status": booking.get("booking_status"),
                        "booking_stay": f"{booking.get('travel_from_date') or ''} -> {booking.get('travel_to_date') or ''}".strip(" ->"),
                        "quality_followup_candidate": quality_followup,
                        "manager_action": (
                            "Follow up: avl_date has passed, checkout is completed, and unit is still not bookable."
                            if quality_followup
                            else "Track as availability-date/current-occupancy inventory; do not count against caretaker quality."
                        ),
                    }
                )
            )
        return rows

    def _compact_availability_quality_followup_rows(self, properties: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        rows = []
        for row in self._availability_quality_followup_properties(properties)[:limit]:
            rows.append(
                compact_dict(
                    {
                        "property": property_unit_display(row),
                        "building": row.get("building_building_name"),
                        "area": row.get("building_area"),
                        "available_from": row.get("available_from_date") or row.get("avl_date"),
                        "checkout_completed": self._checkout_completed_for_availability(row),
                        "bookable": row.get("bookable"),
                        "manager_action": "Follow up because avl_date passed + checkout completed + property still not bookable.",
                    }
                )
            )
        return rows

    def _vacant_since(self, row: dict[str, Any]) -> Any:
        for value in (
            row.get("available_from_date"),
            row.get("avl_date"),
            row.get("check_out"),
            row.get("last_updated_on"),
            row.get("prop_info_last_update_time"),
        ):
            if value not in (None, ""):
                return value

        booking = row.get("current_booking") if isinstance(row.get("current_booking"), dict) else {}
        for value in (
            booking.get("travel_to_date"),
            booking.get("checkout_date"),
            booking.get("actual_checkout_date"),
        ):
            if value not in (None, ""):
                return value
        return None

    def _compact_property_rows(self, properties: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        rows = []
        for row in self._vacant_properties(properties)[:limit]:
            rows.append(
                compact_dict(
                    {
                        "property_name": property_unit_display(row),
                        "vacant_since": self._vacant_since(row),
                    }
                )
            )
        return rows

    def _compact_ticket_rows(self, tickets: Sequence[dict[str, Any]], limit: int, max_text: int = 160) -> list[dict[str, Any]]:
        rows = []
        for row in list(tickets)[:limit]:
            rows.append(
                compact_dict(
                    {
                        "closed_at": row.get("close_date") or row.get("activity_time"),
                        "ticket_id": row.get("source_id"),
                        "category": row.get("category"),
                        "status": row.get("status"),
                        "resolved_by": row.get("resolved_by"),
                        "closed_by": row.get("closed_by"),
                        "days": row.get("active_days"),
                        "cost": row.get("total_cost"),
                        "customer_phone": show_phone(row.get("mobile_number")),
                        "summary": clean_text(row.get("description") or row.get("ticket_feedback"), max_text),
                    }
                )
            )
        return rows

    def _compact_checkin_rows(self, rows_in: Sequence[dict[str, Any]], limit: int, max_text: int = 160) -> list[dict[str, Any]]:
        rows = []
        for row in list(rows_in)[:limit]:
            rows.append(
                compact_dict(
                    {
                        "date": row.get("checkin_date") or row.get("activity_time"),
                        "booking_id": row.get("booking_id"),
                        "stay_rating": row.get("stay_rating"),
                        "cleaning_rating": row.get("cleaning_rating"),
                        "welcome_done": row.get("welcome_flag"),
                        "comment": clean_text(row.get("stay_comment") or row.get("welcome_comment") or row.get("suggestions") or row.get("other_comment"), max_text),
                    }
                )
            )
        return rows

    def _compact_checkout_rows(self, rows_in: Sequence[dict[str, Any]], limit: int, max_text: int = 160) -> list[dict[str, Any]]:
        rows = []
        for row in list(rows_in)[:limit]:
            rows.append(
                compact_dict(
                    {
                        "date": row.get("checkout_date") or row.get("activity_time"),
                        "booking_id": row.get("booking_id"),
                        "rms_rating": row.get("rms_rating"),
                        "building_rating": row.get("building_rating"),
                        "refer_score": row.get("refer_friends_score"),
                        "comment": clean_text(row.get("stay_comment") or row.get("rms_comment") or row.get("suggestions") or row.get("other_comment"), max_text),
                    }
                )
            )
        return rows

    def _compact_site_visit_rows(self, rows_in: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        rows = []

        for row in list(rows_in)[:limit]:
            activity_type = self._site_visit_activity_type(row)
            status_code = row.get("schedule_status")

            rows.append(
                compact_dict(
                    {
                        "time": row.get("site_visit_date") or row.get("activity_time"),
                        "lead_id": row.get("lead_id"),
                        "source_id": row.get("source_id"),
                        "executive": row.get("executive_id"),

                        "building_id": row.get("building_id"),
                        "building_name": row.get("building_name"),
                        "building_caretaker": row.get("building_caretaker"),
                        "prop_id": row.get("prop_id"),
                        "unit_type": row.get("unit_type"),

                        "activity_type": activity_type,
                        "type": row.get("visit_type"),
                        "status": self._site_visit_status_label(status_code),
                        "status_code": status_code,

                        # New not-done evidence fields
                        "not_done_reason": row.get("not_done_reason_candidate"),
                        "overlapping_success_bookings": row.get("overlapping_success_bookings"),
                        "calls_near_visit": row.get("calls_near_visit"),
                        "connected_calls_near_visit": row.get("connected_calls_near_visit"),
                        "missed_calls_near_visit": row.get("missed_calls_near_visit"),
                        "missed_calls_from_caretaker_near_visit": row.get("missed_calls_from_caretaker_near_visit"),
                        "missed_calls_from_customer_near_visit": row.get("missed_calls_from_customer_near_visit"),
                        "missed_calls_unknown_direction_near_visit": row.get("missed_calls_unknown_direction_near_visit"),
                        "non_zero_duration_calls_near_visit": row.get("non_zero_duration_calls_near_visit"),
                        "first_call_near_visit": row.get("first_call_near_visit"),
                        "last_call_near_visit": row.get("last_call_near_visit"),
                        "connected_calls_after_last_missed": row.get("connected_calls_after_last_missed"),
                        "last_missed_call_near_visit": row.get("last_missed_call_near_visit"),
                        "pre_visit_calls_same_day": row.get("pre_visit_calls_same_day"),
                        "pre_visit_connected_calls_same_day": row.get("pre_visit_connected_calls_same_day"),
                        "pre_visit_missed_calls_same_day": row.get("pre_visit_missed_calls_same_day"),
                        "last_pre_visit_connected_call_time": row.get("last_pre_visit_connected_call_time"),
                        "pre_visit_call_minutes_before": row.get("pre_visit_call_minutes_before"),

                        # Lead/customer context if available
                        "customer_phone": row.get("customer_phone"),
                        "customer_phone_alt": row.get("customer_phone_alt"),
                        "lead_raw_status": row.get("lead_raw_status"),
                    }
                )
            )

        return rows

    def _compact_communication_rows(self, calls: Sequence[dict[str, Any]], whatsapp: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in calls:
            rows.append({"line": self._call_line(row)})
        for row in whatsapp:
            rows.append({"line": self._whatsapp_line(row)})
        # Sort using original row time where possible by rebuilding a small lookup.
        combined = list(calls) + list(whatsapp)
        line_time = {}
        for row in combined:
            line = self._call_line(row) if row.get("channel") == "call" else self._whatsapp_line(row)
            line_time[line] = coerce_datetime(row.get("time")) or datetime.min
        rows.sort(key=lambda row: line_time.get(row.get("line"), datetime.min), reverse=True)
        return rows[:limit]

    def _property_mark_rows(
        self,
        properties: Sequence[dict[str, Any]],
        start_dt: Optional[datetime] = None,
        end_dt: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        rows = []
        for row in properties:
            mark_times = [
                coerce_datetime(row.get("last_updated_on")),
                coerce_datetime(row.get("asset_verified_on")),
                coerce_datetime(row.get("flat_verified_on")),
            ]
            mark_times = [value for value in mark_times if value is not None]
            updated_at = max(mark_times) if mark_times else None
            if start_dt is not None and end_dt is not None:
                if updated_at is None or updated_at < start_dt or updated_at >= end_dt:
                    continue

            marks = {
                "checkout_marked": boolish(row.get("mark_check_out")),
                "electricity_bill_marked": boolish(row.get("mark_electricity_bill")),
                "rent_paid_marked": boolish(row.get("mark_rent_paid")),
                "asset_verified": row.get("asset_verified"),
                "flat_verified": row.get("flat_verified"),
            }
            if any(value not in (None, "", False, 0, "0") for value in marks.values()):
                rows.append(
                    compact_dict(
                        {
                            "updated_at": updated_at or row.get("last_updated_on"),
                            **marks,
                            "updated_by": row.get("last_updated_by"),
                        }
                    )
                )
        rows.sort(key=lambda r: coerce_datetime(r.get("updated_at")) or datetime.min, reverse=True)
        return rows

__all__ = ["StaffActivityCollectorService"]
