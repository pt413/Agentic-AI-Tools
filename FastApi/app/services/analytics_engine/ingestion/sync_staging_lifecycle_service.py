from sqlalchemy import text

from app.services.analytics_engine.core.config import SCHEMA_NAME

from .source_db import fetch_all, get_thirdparty_mysql_engine, get_thirdparty_pg_engine
from .sync_base_service import StagingSyncBaseService


class AnalyticsStagingLifecycleSyncService(StagingSyncBaseService):
    def sync_site_visits(self, limit: int = 20000, mode: str = "id"):
        table = "site_visits"
        cp = self.checkpoint.get_checkpoint(table)
        last_id = cp["last_id"]
        last_ts = cp["last_timestamp"]
        source_engine = get_thirdparty_mysql_engine()

        def _chunks(items, size=1000):
            for i in range(0, len(items), size):
                yield items[i : i + size]

        def _source_rows_by_ids(ids):
            out = []
            clean_ids = [int(x) for x in ids if x not in (None, "")]
            for batch_ids in _chunks(clean_ids, 1000):
                if not batch_ids:
                    continue

                holders = ", ".join(f":id_{i}" for i in range(len(batch_ids)))
                params = {f"id_{i}": value for i, value in enumerate(batch_ids)}

                out.extend(
                    fetch_all(
                        source_engine,
                        f"""
                        SELECT *
                        FROM site_visits
                        WHERE id IN ({holders})
                        """,
                        params,
                    )
                )
            return out

        def _payload_from_row(row):
            r = dict(row)
            event_ts = self.safe_dt(r.get("site_visit_date") or r.get("added_on"))
            return {
                "source_id": self.safe_int(r.get("id")),
                "lead_id": self.safe_int(r.get("lead_id")),
                "executive_id": self._clean_text(r.get("added_by")),
                "building_id": self.safe_int(r.get("building_id")),
                "prop_id": self.safe_int(r.get("prop_id")),
                "unit_type": self._clean_text(r.get("unit_type")),
                "schedule_status": self.safe_int(r.get("schedule_status")),
                "visit_type": self._clean_text(r.get("type")),
                "site_visit_date": self.safe_dt(r.get("site_visit_date")),
                "added_on": self.safe_dt(r.get("added_on")),
                "_ts": event_ts,
            }

        # 1) Normal append-only sync: fetch newly created source ids.
        rows = fetch_all(
            source_engine,
            """
            SELECT *
            FROM site_visits
            WHERE id > :last_id
            ORDER BY id
            LIMIT :limit
            """,
            {"last_id": int(last_id or 0), "limit": int(limit)},
        )

        # 2) Status refresh: re-check pending scheduled rows already present in staging.
        # These are old source ids, so they will never come through id > last_id again.
        pending_rows = self.db.execute(
            text(
                f"""
                SELECT source_id
                FROM "{SCHEMA_NAME}".staging_site_visits
                WHERE schedule_status = 1
                AND source_id IS NOT NULL
                ORDER BY source_id
                LIMIT :limit
                """
            ),
            {"limit": int(limit)},
        ).mappings().fetchall()

        pending_ids = [row["source_id"] for row in pending_rows]
        refresh_rows = _source_rows_by_ids(pending_ids)

        payload_by_source_id = {}

        normal_source_ids = set()
        for row in rows:
            item = _payload_from_row(row)
            source_id = item.get("source_id")
            if source_id is None:
                continue
            normal_source_ids.add(source_id)
            payload_by_source_id[source_id] = item

        for row in refresh_rows:
            item = _payload_from_row(row)
            source_id = item.get("source_id")
            if source_id is None:
                continue
            payload_by_source_id[source_id] = item

        payload = list(payload_by_source_id.values())

        if not payload:
            return {
                "inserted_or_updated": 0,
                "status_refreshed": 0,
                "last_id": last_id,
                "last_timestamp": last_ts,
            }

        sql = text(
            f"""
            INSERT INTO "{SCHEMA_NAME}".staging_site_visits AS target (
                source_id, lead_id, executive_id, building_id, prop_id, unit_type,
                schedule_status, visit_type, site_visit_date, added_on, synced_at
            ) VALUES (
                :source_id, :lead_id, :executive_id, :building_id, :prop_id, :unit_type,
                :schedule_status, :visit_type, :site_visit_date, :added_on, NOW()
            )
            ON CONFLICT (source_id) DO UPDATE SET
                lead_id = EXCLUDED.lead_id,
                executive_id = EXCLUDED.executive_id,
                building_id = EXCLUDED.building_id,
                prop_id = EXCLUDED.prop_id,
                unit_type = EXCLUDED.unit_type,
                schedule_status = EXCLUDED.schedule_status,
                visit_type = EXCLUDED.visit_type,
                site_visit_date = EXCLUDED.site_visit_date,
                added_on = EXCLUDED.added_on,
                synced_at = NOW()
            WHERE target.lead_id IS DISTINCT FROM EXCLUDED.lead_id
            OR target.executive_id IS DISTINCT FROM EXCLUDED.executive_id
            OR target.building_id IS DISTINCT FROM EXCLUDED.building_id
            OR target.prop_id IS DISTINCT FROM EXCLUDED.prop_id
            OR target.unit_type IS DISTINCT FROM EXCLUDED.unit_type
            OR target.schedule_status IS DISTINCT FROM EXCLUDED.schedule_status
            OR target.visit_type IS DISTINCT FROM EXCLUDED.visit_type
            OR target.site_visit_date IS DISTINCT FROM EXCLUDED.site_visit_date
            OR target.added_on IS DISTINCT FROM EXCLUDED.added_on
            """
        )

        self._bulk_upsert_in_chunks(sql, payload, 5000)

        # Important:
        # Only new rows should advance the id checkpoint.
        # Refreshed old scheduled rows must not move checkpoint backward/forward.
        new_last_id = max(normal_source_ids, default=last_id or 0)

        normal_payload = [
            row for row in payload
            if row.get("source_id") in normal_source_ids
        ]
        new_last_ts = max(
            (r["_ts"] for r in normal_payload if r.get("_ts") is not None),
            default=last_ts,
        )

        status_refreshed = len(refresh_rows)

        self.checkpoint.update_success(
            table,
            last_id=new_last_id,
            last_timestamp=new_last_ts,
            batch_count=len(payload),
            notes=(
                f"site_visits normal_rows={len(rows)}; "
                f"scheduled_status_refresh_checked={len(pending_ids)}; "
                f"source_refresh_rows={status_refreshed}"
            ),
        )

        return {
            "inserted_or_updated": len(payload),
            "status_refreshed": status_refreshed,
            "last_id": new_last_id,
            "last_timestamp": new_last_ts,
        }
    
    def sync_travel_cart(self, limit: int = 20000, mode: str = "id"):
        table = "travel_cart"
        cp = self.checkpoint.get_checkpoint(table)
        last_id = cp["last_id"]
        last_ts = cp["last_timestamp"]
        source_engine = get_thirdparty_mysql_engine()

        if mode == "time":
            rows = fetch_all(
                source_engine,
                """
                SELECT *
                FROM travel_cart
                WHERE added_on IS NOT NULL
                AND (
                        :last_timestamp IS NULL
                        OR added_on > :last_timestamp
                        OR (added_on = :last_timestamp AND travel_id > :last_id)
                    )
                ORDER BY added_on, travel_id
                LIMIT :limit
                """,
                {"last_timestamp": last_ts, "last_id": int(last_id or 0), "limit": int(limit)},
            )
        else:
            rows = fetch_all(
                source_engine,
                """
                SELECT *
                FROM travel_cart
                WHERE travel_id > :last_id
                ORDER BY travel_id
                LIMIT :limit
                """,
                {"last_id": int(last_id or 0), "limit": int(limit)},
            )

        if not rows:
            return {"inserted_or_updated": 0, "last_id": last_id, "last_timestamp": last_ts}

        payload = []
        for row in rows:
            r = dict(row)
            source_id = self.safe_int(r.get("travel_id"))
            payload.append(
                {
                    "source_id": source_id,
                    "user_id": self.safe_int(r.get("user_id")),
                    "prop_id": self.safe_int(r.get("prop_id")),
                    "travel_from_date": self.safe_dt(r.get("travel_from_date")),
                    "travel_to_date": self.safe_dt(r.get("travel_to_date")),
                    "nights": self.safe_int(r.get("nights")),
                    "booking_type": self._clean_text(r.get("booking_type")),
                    "total_amount": r.get("total_amount"),
                    "advance_amount": r.get("advance_amount"),
                    "pending_amount": r.get("pending_amount"),
                    "source": self._clean_text(r.get("source")),
                    "added_on": self.safe_dt(r.get("added_on")),
                    "bkc_status": self.safe_int(r.get("bkc_status")),
                }
            )

        sql = text(
            f"""
            INSERT INTO "{SCHEMA_NAME}".staging_travel_cart (
                source_id, user_id, prop_id, travel_from_date, travel_to_date,
                nights, booking_type, total_amount, advance_amount, pending_amount,
                source, added_on, bkc_status, synced_at
            ) VALUES (
                :source_id, :user_id, :prop_id, :travel_from_date, :travel_to_date,
                :nights, :booking_type, :total_amount, :advance_amount, :pending_amount,
                :source, :added_on, :bkc_status, (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            )
            ON CONFLICT (source_id) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                prop_id = EXCLUDED.prop_id,
                travel_from_date = EXCLUDED.travel_from_date,
                travel_to_date = EXCLUDED.travel_to_date,
                nights = EXCLUDED.nights,
                booking_type = EXCLUDED.booking_type,
                total_amount = EXCLUDED.total_amount,
                advance_amount = EXCLUDED.advance_amount,
                pending_amount = EXCLUDED.pending_amount,
                source = EXCLUDED.source,
                added_on = EXCLUDED.added_on,
                bkc_status = EXCLUDED.bkc_status,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            """
        )

        self._bulk_upsert_in_chunks(sql, payload, 5000)
        new_last_id = max(r["source_id"] for r in payload if r.get("source_id") is not None)
        new_last_ts = max((r["added_on"] for r in payload if r.get("added_on") is not None), default=last_ts)
        self.checkpoint.update_success(table, last_id=new_last_id, last_timestamp=new_last_ts, batch_count=len(payload))
        return {"inserted_or_updated": len(payload), "last_id": new_last_id, "last_timestamp": new_last_ts}
    def sync_wishlist(self, limit: int = 20000, mode: str = "time"):
        table = "user_wishlist"
        cp = self.checkpoint.get_checkpoint(table)
        last_ts = cp["last_timestamp"]
        source_engine = get_thirdparty_mysql_engine()

        rows = fetch_all(
            source_engine,
            """
            SELECT user_id, prop_id, added_on
            FROM user_wishlist
            WHERE added_on IS NOT NULL
            AND (:last_timestamp IS NULL OR added_on > :last_timestamp)
            ORDER BY added_on, user_id, prop_id
            LIMIT :limit
            """,
            {"last_timestamp": last_ts, "limit": int(limit)},
        )

        if not rows:
            return {"inserted_or_updated": 0, "last_id": cp["last_id"], "last_timestamp": last_ts}

        payload = []
        for row in rows:
            r = dict(row)
            added_on = self.safe_dt(r.get("added_on"))
            user_id = self.safe_int(r.get("user_id"))
            prop_id = self.safe_int(r.get("prop_id"))

            payload.append(
                {
                    "source_id": self.stable_bigint(user_id, prop_id, added_on),
                    "user_id": user_id,
                    "prop_id": prop_id,
                    "added_on": added_on,
                }
            )

        sql = text(
            f"""
            INSERT INTO "{SCHEMA_NAME}".staging_user_wishlist (
                source_id, user_id, prop_id, added_on, synced_at
            ) VALUES (
                :source_id, :user_id, :prop_id, :added_on, (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            )
            ON CONFLICT (user_id, prop_id, added_on) DO UPDATE SET
                source_id = EXCLUDED.source_id,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            """
        )

        self._bulk_upsert_in_chunks(sql, payload, 5000)

        new_last_ts = max(
            (r["added_on"] for r in payload if r.get("added_on") is not None),
            default=last_ts,
        )
        new_last_id = max(
            (r["source_id"] for r in payload if r.get("source_id") is not None),
            default=cp["last_id"],
        )

        self.checkpoint.update_success(
            table,
            last_id=new_last_id,
            last_timestamp=new_last_ts,
            batch_count=len(payload),
        )

        return {
            "inserted_or_updated": len(payload),
            "last_id": new_last_id,
            "last_timestamp": new_last_ts,
        }
    def sync_user_contact_info(self, limit: int = 20000, mode: str = "id"):
        table = "user_contact_info"
        cp = self.checkpoint.get_checkpoint(table)
        last_id = cp["last_id"]
        last_ts = cp["last_timestamp"]
        source_engine = get_thirdparty_mysql_engine()

        if mode == "time":
            rows = fetch_all(
                source_engine,
                """
                SELECT
                    id,
                    user_id,
                    booking_id,
                    email,
                    name,
                    mobile,
                    added_by,
                    added_on
                FROM user_contact_info
                WHERE added_on IS NOT NULL
                  AND (
                        :last_timestamp IS NULL
                        OR added_on > :last_timestamp
                        OR (added_on = :last_timestamp AND id > :last_id)
                      )
                ORDER BY added_on, id
                LIMIT :limit
                """,
                {
                    "last_timestamp": last_ts,
                    "last_id": int(last_id or 0),
                    "limit": int(limit),
                },
            )
        else:
            rows = fetch_all(
                source_engine,
                """
                SELECT
                    id,
                    user_id,
                    booking_id,
                    email,
                    name,
                    mobile,
                    added_by,
                    added_on
                FROM user_contact_info
                WHERE id > :last_id
                ORDER BY id
                LIMIT :limit
                """,
                {"last_id": int(last_id or 0), "limit": int(limit)},
            )

        if not rows:
            return {
                "inserted_or_updated": 0,
                "last_id": last_id,
                "last_timestamp": last_ts,
            }

        payload = []
        for row in rows:
            r = dict(row)
            mobile_raw = self._clean_text(r.get("mobile"))
            added_on = self.safe_dt(r.get("added_on"))
            payload.append(
                {
                    "source_id": self.safe_int(r.get("id")),
                    "user_id": self.safe_int(r.get("user_id")),
                    "booking_id": self.safe_int(r.get("booking_id")),
                    "email": self._clean_lower_email(r.get("email")),
                    "contact_name": self._clean_text(r.get("name")),
                    "mobile": mobile_raw,
                    "normalized_mobile": self.norm_phone(mobile_raw),
                    "added_by": self._clean_text(r.get("added_by")),
                    "added_on": added_on,
                    "_ts": added_on,
                }
            )

        sql = text(
            f"""
            INSERT INTO "{SCHEMA_NAME}".staging_user_contact_info (
                source_id, user_id, booking_id, email, contact_name,
                mobile, normalized_mobile, added_by, added_on, synced_at
            ) VALUES (
                :source_id, :user_id, :booking_id, :email, :contact_name,
                :mobile, :normalized_mobile, :added_by, :added_on, (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            )
            ON CONFLICT (source_id) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                booking_id = EXCLUDED.booking_id,
                email = EXCLUDED.email,
                contact_name = EXCLUDED.contact_name,
                mobile = EXCLUDED.mobile,
                normalized_mobile = EXCLUDED.normalized_mobile,
                added_by = EXCLUDED.added_by,
                added_on = EXCLUDED.added_on,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            """
        )

        self._bulk_upsert_in_chunks(sql, payload, 5000)
        new_last_id = max(r["source_id"] for r in payload if r.get("source_id") is not None)
        new_last_ts = max((r["_ts"] for r in payload if r.get("_ts") is not None), default=last_ts)
        self.checkpoint.update_success(
            table,
            last_id=new_last_id,
            last_timestamp=new_last_ts,
            batch_count=len(payload),
        )
        return {
            "inserted_or_updated": len(payload),
            "last_id": new_last_id,
            "last_timestamp": new_last_ts,
        }

    def sync_booking_confirm(self, limit: int = 5000, mode: str = "id"):
        table = "booking_confirm"
        cp = self.checkpoint.get_checkpoint(table)
        last_id = cp["last_id"]
        last_ts = cp["last_timestamp"]
        source_engine = get_thirdparty_mysql_engine()

        def _chunks(items, size=1000):
            for i in range(0, len(items), size):
                yield items[i : i + size]

        def _source_rows_by_booking_ids(ids):
            out = []
            clean_ids = [int(x) for x in ids if x not in (None, "")]
            for batch_ids in _chunks(clean_ids, 1000):
                if not batch_ids:
                    continue

                holders = ", ".join(f":id_{i}" for i in range(len(batch_ids)))
                params = {f"id_{i}": value for i, value in enumerate(batch_ids)}

                out.extend(
                    fetch_all(
                        source_engine,
                        f"""
                        SELECT
                            booking_id,
                            user_id,
                            lead_id,
                            prop_id,
                            booking_status,
                            booking_type,
                            booking_source,
                            txn_source,
                            travel_from_date,
                            travel_to_date,
                            total_amount,
                            early_cout,
                            before_disc_monthly,
                            after_disc_month_rent,
                            num_guests,
                            renv_gst,
                            rent_margin,
                            booking_datetime,
                            created_by,
                            extend_status,
                            extend_status_time_mark
                        FROM booking_confirm
                        WHERE booking_id IN ({holders})
                        """,
                        params,
                    )
                )
            return out

        def _payload_from_row(row):
            r = dict(row)
            booking_datetime = self.safe_dt(r.get("booking_datetime"))

            return {
                "source_id": self.safe_int(r.get("booking_id")),
                "booking_id": self.safe_int(r.get("booking_id")),
                "user_id": self.safe_int(r.get("user_id")),
                "lead_id": self.safe_int(r.get("lead_id")),
                "prop_id": self.safe_int(r.get("prop_id")),
                "booking_status": self._clean_text(r.get("booking_status")),
                "booking_type": self._clean_text(r.get("booking_type")),
                "booking_source": self._clean_text(r.get("booking_source")),
                "txn_source": self._clean_text(r.get("txn_source")),
                "travel_from_date": self.safe_dt(r.get("travel_from_date")),
                "travel_to_date": self.safe_dt(r.get("travel_to_date")),
                "total_amount": self.safe_numeric(r.get("total_amount")),
                "early_cout": self.safe_dt(r.get("early_cout")),
                "before_disc_monthly": self.safe_numeric(r.get("before_disc_monthly")),
                "after_disc_month_rent": self.safe_numeric(r.get("after_disc_month_rent")),
                "num_guests": self.safe_int(r.get("num_guests")),
                "renv_gst": self.safe_numeric(r.get("renv_gst")),
                "rent_margin": self.safe_numeric(r.get("rent_margin")),
                "booking_datetime": booking_datetime,
                "created_by": self._clean_text(r.get("created_by")),
                "extend_status": self.safe_int(r.get("extend_status")),
                "extend_status_time_mark": self.safe_dt(r.get("extend_status_time_mark")),
                "_ts": booking_datetime,
            }

        # 1) Normal append-only sync for new bookings.
        if mode == "time":
            rows = fetch_all(
                source_engine,
                """
                SELECT
                    booking_id,
                    user_id,
                    lead_id,
                    prop_id,
                    booking_status,
                    booking_type,
                    booking_source,
                    txn_source,
                    travel_from_date,
                    travel_to_date,
                    total_amount,
                    early_cout,
                    before_disc_monthly,
                    after_disc_month_rent,
                    num_guests,
                    renv_gst,
                    rent_margin,
                    booking_datetime,
                    created_by,
                    extend_status,
                    extend_status_time_mark
                FROM booking_confirm
                WHERE booking_datetime IS NOT NULL
                AND (
                        :last_timestamp IS NULL
                        OR booking_datetime > :last_timestamp
                        OR (booking_datetime = :last_timestamp AND booking_id > :last_id)
                    )
                ORDER BY booking_datetime, booking_id
                LIMIT :limit
                """,
                {
                    "last_timestamp": last_ts,
                    "last_id": int(last_id or 0),
                    "limit": int(limit),
                },
            )
        else:
            rows = fetch_all(
                source_engine,
                """
                SELECT
                    booking_id,
                    user_id,
                    lead_id,
                    prop_id,
                    booking_status,
                    booking_type,
                    booking_source,
                    txn_source,
                    travel_from_date,
                    travel_to_date,
                    total_amount,
                    early_cout,
                    before_disc_monthly,
                    after_disc_month_rent,
                    num_guests,
                    renv_gst,
                    rent_margin,
                    booking_datetime,
                    created_by,
                    extend_status,
                    extend_status_time_mark
                FROM booking_confirm
                WHERE booking_id > :last_id
                ORDER BY booking_id
                LIMIT :limit
                """,
                {"last_id": int(last_id or 0), "limit": int(limit)},
            )

        # 2) Refresh old bookings that look extension-related in staging.
        # These old booking_ids will never come again through booking_id > last_id.
        refresh_candidates = self.db.execute(
            text(
                f"""
                SELECT source_id
                FROM "{SCHEMA_NAME}".staging_booking_confirm
                WHERE source_id IS NOT NULL
                AND (
                        COALESCE(extend_status, 0) <> 0
                        OR extend_status_time_mark IS NOT NULL
                    )
                ORDER BY source_id
                LIMIT :limit
                """
            ),
            {"limit": int(limit)},
        ).mappings().fetchall()

        refresh_ids = [row["source_id"] for row in refresh_candidates]
        refresh_rows = _source_rows_by_booking_ids(refresh_ids)

        payload_by_source_id = {}
        normal_source_ids = set()

        for row in rows:
            item = _payload_from_row(row)
            source_id = item.get("source_id")
            if source_id is None:
                continue
            normal_source_ids.add(source_id)
            payload_by_source_id[source_id] = item

        for row in refresh_rows:
            item = _payload_from_row(row)
            source_id = item.get("source_id")
            if source_id is None:
                continue
            payload_by_source_id[source_id] = item

        payload = list(payload_by_source_id.values())

        if not payload:
            return {
                "inserted_or_updated": 0,
                "extension_refreshed": 0,
                "last_id": last_id,
                "last_timestamp": last_ts,
            }

        sql = text(
            f"""
            INSERT INTO "{SCHEMA_NAME}".staging_booking_confirm AS target (
                source_id,
                booking_id,
                user_id,
                lead_id,
                prop_id,
                booking_status,
                booking_type,
                booking_source,
                txn_source,
                travel_from_date,
                travel_to_date,
                total_amount,
                early_cout,
                before_disc_monthly,
                after_disc_month_rent,
                num_guests,
                renv_gst,
                rent_margin,
                booking_datetime,
                created_by,
                extend_status,
                extend_status_time_mark,
                synced_at
            ) VALUES (
                :source_id,
                :booking_id,
                :user_id,
                :lead_id,
                :prop_id,
                :booking_status,
                :booking_type,
                :booking_source,
                :txn_source,
                :travel_from_date,
                :travel_to_date,
                :total_amount,
                :early_cout,
                :before_disc_monthly,
                :after_disc_month_rent,
                :num_guests,
                :renv_gst,
                :rent_margin,
                :booking_datetime,
                :created_by,
                :extend_status,
                :extend_status_time_mark,
                (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            )
            ON CONFLICT (source_id) DO UPDATE SET
                booking_id = EXCLUDED.booking_id,
                user_id = EXCLUDED.user_id,
                lead_id = EXCLUDED.lead_id,
                prop_id = EXCLUDED.prop_id,
                booking_status = EXCLUDED.booking_status,
                booking_type = EXCLUDED.booking_type,
                booking_source = EXCLUDED.booking_source,
                txn_source = EXCLUDED.txn_source,
                travel_from_date = EXCLUDED.travel_from_date,
                travel_to_date = EXCLUDED.travel_to_date,
                total_amount = EXCLUDED.total_amount,
                early_cout = EXCLUDED.early_cout,
                before_disc_monthly = EXCLUDED.before_disc_monthly,
                after_disc_month_rent = EXCLUDED.after_disc_month_rent,
                num_guests = EXCLUDED.num_guests,
                renv_gst = EXCLUDED.renv_gst,
                rent_margin = EXCLUDED.rent_margin,
                booking_datetime = EXCLUDED.booking_datetime,
                created_by = EXCLUDED.created_by,
                extend_status = EXCLUDED.extend_status,
                extend_status_time_mark = EXCLUDED.extend_status_time_mark,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            WHERE target.booking_id IS DISTINCT FROM EXCLUDED.booking_id
            OR target.user_id IS DISTINCT FROM EXCLUDED.user_id
            OR target.lead_id IS DISTINCT FROM EXCLUDED.lead_id
            OR target.prop_id IS DISTINCT FROM EXCLUDED.prop_id
            OR target.booking_status IS DISTINCT FROM EXCLUDED.booking_status
            OR target.booking_type IS DISTINCT FROM EXCLUDED.booking_type
            OR target.booking_source IS DISTINCT FROM EXCLUDED.booking_source
            OR target.txn_source IS DISTINCT FROM EXCLUDED.txn_source
            OR target.travel_from_date IS DISTINCT FROM EXCLUDED.travel_from_date
            OR target.travel_to_date IS DISTINCT FROM EXCLUDED.travel_to_date
            OR target.total_amount IS DISTINCT FROM EXCLUDED.total_amount
            OR target.early_cout IS DISTINCT FROM EXCLUDED.early_cout
            OR target.before_disc_monthly IS DISTINCT FROM EXCLUDED.before_disc_monthly
            OR target.after_disc_month_rent IS DISTINCT FROM EXCLUDED.after_disc_month_rent
            OR target.num_guests IS DISTINCT FROM EXCLUDED.num_guests
            OR target.renv_gst IS DISTINCT FROM EXCLUDED.renv_gst
            OR target.rent_margin IS DISTINCT FROM EXCLUDED.rent_margin
            OR target.booking_datetime IS DISTINCT FROM EXCLUDED.booking_datetime
            OR target.created_by IS DISTINCT FROM EXCLUDED.created_by
            OR target.extend_status IS DISTINCT FROM EXCLUDED.extend_status
            OR target.extend_status_time_mark IS DISTINCT FROM EXCLUDED.extend_status_time_mark
            """
        )

        self._bulk_upsert_in_chunks(sql, payload, 5000)

        # Only normal new rows should advance checkpoint.
        # Refresh rows are old bookings and must not affect last_id.
        new_last_id = max(normal_source_ids, default=last_id or 0)

        normal_payload = [
            row for row in payload
            if row.get("source_id") in normal_source_ids
        ]

        new_last_ts = max(
            (r["_ts"] for r in normal_payload if r.get("_ts") is not None),
            default=last_ts,
        )

        self.checkpoint.update_success(
            table,
            last_id=new_last_id,
            last_timestamp=new_last_ts,
            batch_count=len(payload),
            notes=(
                f"booking_confirm normal_rows={len(rows)}; "
                f"extension_refresh_checked={len(refresh_ids)}; "
                f"source_refresh_rows={len(refresh_rows)}"
            ),
        )

        return {
            "inserted_or_updated": len(payload),
            "extension_refreshed": len(refresh_rows),
            "last_id": new_last_id,
            "last_timestamp": new_last_ts,
        }
    def sync_web_visits(self, limit: int = 100000, mode: str = "id"):
        table = "web_visits"
        cp = self.checkpoint.get_checkpoint(table)
        last_id = cp["last_id"]
        source_engine = get_thirdparty_mysql_engine()

        rows = fetch_all(
            source_engine,
            """
            SELECT
                id,
                referal_page,
                current_page,
                ip_address,
                session_id,
                user_id,
                lead_id,
                prop_id,
                source,
                user_agent
            FROM last_visited
            WHERE id > :last_id
            ORDER BY id
            LIMIT :limit
            """,
            {"last_id": int(last_id or 0), "limit": int(limit)},
        )

        if not rows:
            return {"inserted_or_updated": 0, "last_id": last_id, "last_timestamp": cp["last_timestamp"]}

        payload = []
        for row in rows:
            r = dict(row)
            payload.append(
                {
                    "source_id": self.safe_int(r.get("id")),
                    "referal_page": self._clean_text(r.get("referal_page")),
                    "current_page": self._clean_text(r.get("current_page")),
                    "ip_address": self._clean_text(r.get("ip_address")),
                    "session_id": self._clean_text(r.get("session_id")),
                    "user_id": self.safe_int(r.get("user_id")),
                    "lead_id": self.safe_int(r.get("lead_id")),
                    "prop_id": self.safe_int(r.get("prop_id")),
                    "source": self._clean_text(r.get("source")),
                    "user_agent": self._clean_text(r.get("user_agent")),
                }
            )

        sql = text(
            f"""
            INSERT INTO \"{SCHEMA_NAME}\".staging_web_visits (
                source_id,
                referal_page,
                current_page,
                ip_address,
                session_id,
                user_id,
                lead_id,
                prop_id,
                source,
                user_agent,
                synced_at
            ) VALUES (
                :source_id,
                :referal_page,
                :current_page,
                :ip_address,
                :session_id,
                :user_id,
                :lead_id,
                :prop_id,
                :source,
                :user_agent,
                (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            )
            ON CONFLICT (source_id) DO UPDATE SET
                referal_page = EXCLUDED.referal_page,
                current_page = EXCLUDED.current_page,
                ip_address = EXCLUDED.ip_address,
                session_id = EXCLUDED.session_id,
                user_id = EXCLUDED.user_id,
                lead_id = EXCLUDED.lead_id,
                prop_id = EXCLUDED.prop_id,
                source = EXCLUDED.source,
                user_agent = EXCLUDED.user_agent,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            """
        )

        self._bulk_upsert_in_chunks(sql, payload, 5000)
        new_last_id = max(r["source_id"] for r in payload if r.get("source_id") is not None)
        self.checkpoint.update_success(table, last_id=new_last_id, batch_count=len(payload))
        return {"inserted_or_updated": len(payload), "last_id": new_last_id, "last_timestamp": cp["last_timestamp"]}

    def sync_whatsapp_messages(self, limit: int = 50000, mode: str = "time"):
        table = "whatsapp_messages"
        cp = self.checkpoint.get_checkpoint(table)
        last_id = cp["last_id"]
        last_ts = cp["last_timestamp"]
        source_last_ts = self.source_utc_cursor_from_ist(last_ts)
        source_engine = get_thirdparty_pg_engine()

        if mode == "time":
            rows = fetch_all(
                source_engine,
                """
                SELECT
                    message_id,
                    admin_number,
                    cx_number,
                    direction,
                    timestamp,
                    message_type,
                    clean_content,
                    extracted_text,
                    remote_jid,
                    issent,
                    isread,
                    LENGTH(
                        COALESCE(
                            NULLIF(clean_content, ''),
                            NULLIF(extracted_text, ''),
                            ''
                        )
                    ) AS content_length
                FROM public.messages
                WHERE timestamp IS NOT NULL
                AND (
                        :last_timestamp IS NULL
                        OR timestamp > :last_timestamp
                        OR (timestamp = :last_timestamp AND message_id > :last_message_id)
                    )
                ORDER BY timestamp, message_id
                LIMIT :limit
                """,
                {
                    "last_timestamp": source_last_ts,
                    "last_message_id": str(last_id or ""),
                    "limit": int(limit),
                },
            )
        else:
            rows = fetch_all(
                source_engine,
                """
                SELECT
                    message_id,
                    admin_number,
                    cx_number,
                    direction,
                    timestamp,
                    message_type,
                    clean_content,
                    remote_jid,
                    issent,
                    isread,
                    LENGTH(clean_content) AS content_length
                FROM public.messages
                WHERE message_id > :last_message_id
                ORDER BY message_id
                LIMIT :limit
                """,
                {
                    "last_message_id": str(last_id or ""),
                    "limit": int(limit),
                },
            )

        if not rows:
            return {"inserted_or_updated": 0, "last_id": last_id, "last_timestamp": last_ts}

        payload = []
        for row in rows:
            r = dict(row)
            payload.append(
                {
                    "source_id": self._clean_text(r.get("message_id")),
                    "lead_id": None,
                    "executive_id": None,
                    "cx_number": self.norm_phone(r.get("cx_number")),
                    "admin_number": self.norm_phone(r.get("admin_number")),
                    "message_time": self.safe_dt_from_utc(r.get("timestamp")),
                    "direction": (self._clean_text(r.get("direction")) or "").lower() or None,
                    "message_type": self._clean_text(r.get("message_type")),
                    "clean_content": r.get("clean_content"),
                    "extracted_text": r.get("extracted_text"),
                    "remote_jid": self._clean_text(r.get("remote_jid")),
                    "issent": r.get("issent"),
                    "isread": r.get("isread"),
                    "content_length": self.safe_int(r.get("content_length")) or 0,
                }
            )

        sql = text(
            f"""
            INSERT INTO "{SCHEMA_NAME}".staging_whatsapp_messages (
                source_id, lead_id, executive_id, cx_number, admin_number, message_time,
                direction, message_type, clean_content,  extracted_text, remote_jid,
                issent, isread, content_length, synced_at
            ) VALUES (
                :source_id, :lead_id, :executive_id, :cx_number, :admin_number, :message_time,
                :direction, :message_type, :clean_content, :extracted_text, :remote_jid,
                :issent, :isread, :content_length, (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            )
            ON CONFLICT (source_id) DO UPDATE SET
                lead_id = EXCLUDED.lead_id,
                executive_id = EXCLUDED.executive_id,
                cx_number = EXCLUDED.cx_number,
                admin_number = EXCLUDED.admin_number,
                message_time = EXCLUDED.message_time,
                direction = EXCLUDED.direction,
                message_type = EXCLUDED.message_type,
                clean_content = EXCLUDED.clean_content,
                extracted_text = EXCLUDED.extracted_text,
                remote_jid = EXCLUDED.remote_jid,
                issent = EXCLUDED.issent,
                isread = EXCLUDED.isread,
                content_length = EXCLUDED.content_length,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            """
        )

        self._bulk_upsert_in_chunks(sql, payload, 3000)

        new_last_ts = max(
            (r["message_time"] for r in payload if r.get("message_time") is not None),
            default=last_ts,
        )
        new_last_id = max(
            (r["source_id"] for r in payload if r.get("source_id") is not None),
            default=last_id,
        )

        self.checkpoint.update_success(
            table,
            last_id=new_last_id,
            last_timestamp=new_last_ts,
            batch_count=len(payload),
        )

        return {
            "inserted_or_updated": len(payload),
            "last_id": new_last_id,
            "last_timestamp": new_last_ts,
        } 
    def sync_booking_audit_history(self, limit: int = 50000, mode: str = "id"):
        table = "booking_audit_history"
        cp = self.checkpoint.get_checkpoint(table)
        last_id = cp["last_id"]
        last_ts = cp["last_timestamp"]
        source_engine = get_thirdparty_mysql_engine()

        if mode == "time":
            rows = fetch_all(
                source_engine,
                """
                SELECT id, booking_id, updated_by, audit_history, added_time
                FROM booking_audit_history
                WHERE added_time IS NOT NULL
                  AND (
                        :last_timestamp IS NULL
                        OR added_time > :last_timestamp
                        OR (added_time = :last_timestamp AND id > :last_id)
                      )
                ORDER BY added_time, id
                LIMIT :limit
                """,
                {"last_timestamp": last_ts, "last_id": int(last_id or 0), "limit": int(limit)},
            )
        else:
            rows = fetch_all(
                source_engine,
                """
                SELECT id, booking_id, updated_by, audit_history, added_time
                FROM booking_audit_history
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
            payload.append(
                {
                    "source_id": self.safe_int(r.get("id")),
                    "booking_id": self.safe_int(r.get("booking_id")),
                    "updated_by": self._clean_text(r.get("updated_by")),
                    "audit_history": r.get("audit_history"),
                    "added_time": self.safe_dt(r.get("added_time")),
                }
            )

        sql = text(
            f"""
            INSERT INTO \"{SCHEMA_NAME}\".staging_booking_audit_history (
                source_id, booking_id, updated_by, audit_history, added_time, synced_at
            ) VALUES (
                :source_id, :booking_id, :updated_by, :audit_history, :added_time, (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            )
            ON CONFLICT (source_id) DO UPDATE SET
                booking_id = EXCLUDED.booking_id,
                updated_by = EXCLUDED.updated_by,
                audit_history = EXCLUDED.audit_history,
                added_time = EXCLUDED.added_time,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            """
        )

        self._bulk_upsert_in_chunks(sql, payload, 5000)
        new_last_id = max(r["source_id"] for r in payload if r.get("source_id") is not None)
        new_last_ts = max((r["added_time"] for r in payload if r.get("added_time") is not None), default=last_ts)
        self.checkpoint.update_success(table, last_id=new_last_id, last_timestamp=new_last_ts, batch_count=len(payload))
        return {"inserted_or_updated": len(payload), "last_id": new_last_id, "last_timestamp": new_last_ts}

    def sync_booking_invoice_details(self, limit: int = 50000, mode: str = "id"):
        table = "booking_invoice_details"
        cp = self.checkpoint.get_checkpoint(table)
        last_id = cp["last_id"]
        last_ts = cp["last_timestamp"]
        source_engine = get_thirdparty_mysql_engine()

        if mode == "time":
            rows = fetch_all(
                source_engine,
                """
                SELECT
                    invoice_id,
                    booking_id,
                    payment_id,
                    amount_status,
                    duration_period,
                    mail_status,
                    sa_mail_status,
                    reminder_mail,
                    amount_recieved,
                    amount,
                    total_amount,
                    disc,
                    from_date,
                    till_date,
                    pending_balance,
                    payment_mode,
                    comment,
                    status,
                    mail_count,
                    send_time,
                    modify,
                    Transaction_Type,
                    Created_on,
                    utr_no,
                    utr_added_by,
                    om_rent,
                    sa_rent,
                    rent_receipt_dw,
                    sa_receipt_dw,
                    utr_added_on
                FROM booking_invoice_details
                WHERE COALESCE(utr_added_on, send_time, Created_on) IS NOT NULL
                AND (
                        :last_timestamp IS NULL
                        OR COALESCE(utr_added_on, send_time, Created_on) > :last_timestamp
                        OR (
                            COALESCE(utr_added_on, send_time, Created_on) = :last_timestamp
                            AND invoice_id > :last_id
                        )
                    )
                ORDER BY COALESCE(utr_added_on, send_time, Created_on), invoice_id
                LIMIT :limit
                """,
                {"last_timestamp": last_ts, "last_id": int(last_id or 0), "limit": int(limit)},
            )
        else:
            rows = fetch_all(
                source_engine,
                """
                SELECT
                    invoice_id,
                    booking_id,
                    payment_id,
                    amount_status,
                    duration_period,
                    mail_status,
                    sa_mail_status,
                    reminder_mail,
                    amount_recieved,
                    amount,
                    total_amount,
                    disc,
                    from_date,
                    till_date,
                    pending_balance,
                    payment_mode,
                    comment,
                    status,
                    mail_count,
                    send_time,
                    modify,
                    Transaction_Type,
                    Created_on,
                    utr_no,
                    utr_added_by,
                    om_rent,
                    sa_rent,
                    rent_receipt_dw,
                    sa_receipt_dw,
                    utr_added_on
                FROM booking_invoice_details
                WHERE invoice_id > :last_id
                ORDER BY invoice_id
                LIMIT :limit
                """,
                {"last_id": int(last_id or 0), "limit": int(limit)},
            )

        if not rows:
            return {"inserted_or_updated": 0, "last_id": last_id, "last_timestamp": last_ts}

        payload = []
        for row in rows:
            r = dict(row)
            ts = self.safe_dt(r.get("utr_added_on") or r.get("send_time") or r.get("Created_on"))
            payload.append(
                {
                    "source_id": self.safe_int(r.get("invoice_id")),
                    "booking_id": self.safe_int(r.get("booking_id")),
                    "payment_id": self.safe_int(r.get("payment_id")),
                    "amount_status": self._clean_text(r.get("amount_status")),
                    "duration_period": self._clean_text(r.get("duration_period")),
                    "mail_status": self.safe_int(r.get("mail_status")),
                    "sa_mail_status": self.safe_int(r.get("sa_mail_status")),
                    "reminder_mail": self.safe_int(r.get("reminder_mail")),
                    "amount_recieved": self.safe_numeric(r.get("amount_recieved")),
                    "amount": self.safe_numeric(r.get("amount")),
                    "total_amount": self.safe_numeric(r.get("total_amount")),
                    "disc": self.safe_numeric(r.get("disc")),
                    "from_date": self.safe_dt(r.get("from_date")),
                    "till_date": self.safe_dt(r.get("till_date")),
                    "pending_balance": self.safe_numeric(r.get("pending_balance")),
                    "payment_mode": self._clean_text(r.get("payment_mode")),
                    "comment": self._clean_text(r.get("comment")),
                    "status": self._clean_text(r.get("status")),
                    "mail_count": self.safe_int(r.get("mail_count")),
                    "send_time": self.safe_dt(r.get("send_time")),
                    "modify_flag": self.safe_int(r.get("modify")),
                    "transaction_type": self._clean_text(r.get("Transaction_Type")),
                    "created_on": self.safe_dt(r.get("Created_on")),
                    "utr_no": self._clean_text(r.get("utr_no")),
                    "utr_added_by": self._clean_text(r.get("utr_added_by")),
                    "om_rent": self.safe_numeric(r.get("om_rent")),
                    "sa_rent": self.safe_numeric(r.get("sa_rent")),
                    "rent_receipt_dw": self.safe_int(r.get("rent_receipt_dw")),
                    "sa_receipt_dw": self.safe_int(r.get("sa_receipt_dw")),
                    "utr_added_on": self.safe_dt(r.get("utr_added_on")),
                    "_ts": ts,
                }
            )

        sql = text(
            f"""
            INSERT INTO "{SCHEMA_NAME}".staging_booking_invoice_details (
                source_id,
                booking_id,
                payment_id,
                amount_status,
                duration_period,
                mail_status,
                sa_mail_status,
                reminder_mail,
                amount_recieved,
                amount,
                total_amount,
                disc,
                from_date,
                till_date,
                pending_balance,
                payment_mode,
                comment,
                status,
                mail_count,
                send_time,
                modify_flag,
                transaction_type,
                created_on,
                utr_no,
                utr_added_by,
                om_rent,
                sa_rent,
                rent_receipt_dw,
                sa_receipt_dw,
                utr_added_on,
                synced_at
            ) VALUES (
                :source_id,
                :booking_id,
                :payment_id,
                :amount_status,
                :duration_period,
                :mail_status,
                :sa_mail_status,
                :reminder_mail,
                :amount_recieved,
                :amount,
                :total_amount,
                :disc,
                :from_date,
                :till_date,
                :pending_balance,
                :payment_mode,
                :comment,
                :status,
                :mail_count,
                :send_time,
                :modify_flag,
                :transaction_type,
                :created_on,
                :utr_no,
                :utr_added_by,
                :om_rent,
                :sa_rent,
                :rent_receipt_dw,
                :sa_receipt_dw,
                :utr_added_on,
                (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            )
            ON CONFLICT (source_id) DO UPDATE SET
                booking_id = EXCLUDED.booking_id,
                payment_id = EXCLUDED.payment_id,
                amount_status = EXCLUDED.amount_status,
                duration_period = EXCLUDED.duration_period,
                mail_status = EXCLUDED.mail_status,
                sa_mail_status = EXCLUDED.sa_mail_status,
                reminder_mail = EXCLUDED.reminder_mail,
                amount_recieved = EXCLUDED.amount_recieved,
                amount = EXCLUDED.amount,
                total_amount = EXCLUDED.total_amount,
                disc = EXCLUDED.disc,
                from_date = EXCLUDED.from_date,
                till_date = EXCLUDED.till_date,
                pending_balance = EXCLUDED.pending_balance,
                payment_mode = EXCLUDED.payment_mode,
                comment = EXCLUDED.comment,
                status = EXCLUDED.status,
                mail_count = EXCLUDED.mail_count,
                send_time = EXCLUDED.send_time,
                modify_flag = EXCLUDED.modify_flag,
                transaction_type = EXCLUDED.transaction_type,
                created_on = EXCLUDED.created_on,
                utr_no = EXCLUDED.utr_no,
                utr_added_by = EXCLUDED.utr_added_by,
                om_rent = EXCLUDED.om_rent,
                sa_rent = EXCLUDED.sa_rent,
                rent_receipt_dw = EXCLUDED.rent_receipt_dw,
                sa_receipt_dw = EXCLUDED.sa_receipt_dw,
                utr_added_on = EXCLUDED.utr_added_on,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            """
        )

        self._bulk_upsert_in_chunks(sql, payload, 5000)
        new_last_id = max(r["source_id"] for r in payload if r.get("source_id") is not None)
        new_last_ts = max((r["_ts"] for r in payload if r.get("_ts") is not None), default=last_ts)
        self.checkpoint.update_success(table, last_id=new_last_id, last_timestamp=new_last_ts, batch_count=len(payload))
        return {"inserted_or_updated": len(payload), "last_id": new_last_id, "last_timestamp": new_last_ts}