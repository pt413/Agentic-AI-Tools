import json

from sqlalchemy import text

from app.services.analytics_engine.core.config import SCHEMA_NAME

from .source_db import fetch_all, get_thirdparty_mysql_engine, get_thirdparty_pg_engine
from .sync_base_service import StagingSyncBaseService


class AnalyticsStagingCrmSyncService(StagingSyncBaseService):
    def _clean_team(self, val):
        team = self._clean_text(val)
        if team is None:
            return None

        if str(team).strip().lower() in {"0", "null", "none", "na", "n/a"}:
            return None

        return team

    def _caretaker_active_from_source_team(self, val):
        """Return the staging status for an existing caretaker."""
        team = self._clean_team(val)
        if team is not None and str(team).strip().lower() == "caretaker":
            return "Active"
        return "Inactive"


    def _ensure_building_property_tables(self):
        """Ensure building/property master staging tables exist and have required columns."""
        building_columns = {
            "building_id": "BIGINT",
            "building_name": "TEXT",
            "city": "TEXT",
            "area": "TEXT",
            "address": "TEXT",
            "pincode": "INTEGER",
            "glat": "NUMERIC(10, 8)",
            "glng": "NUMERIC(11, 8)",
            "direction_note": "TEXT",
            "caretaker": "TEXT",
            "supervisor": "TEXT",
            "ops_manager": "TEXT",
            "finance_supervisor": "TEXT",
            "sales": "TEXT",
            "sales_phone": "TEXT",
            "sales_normalized_phone": "TEXT",
            "marketing": "TEXT",
            "building_status": "INTEGER",
            "show_tenants": "BOOLEAN",
            "rent_model": "TEXT",
            "future_booking_plan": "INTEGER",
            "agreement_date": "TIMESTAMP NULL",
            "agreement_renewable_date": "TIMESTAMP NULL",
            "position_date": "TIMESTAMP NULL",
            "grace_period": "INTEGER",
            "wifi_account_id": "TEXT",
            "wifi_details": "TEXT",
            "wifi_comment": "TEXT",
            "wifi_expire_on": "DATE",
            "wifi_recharge": "INTEGER",
            "updated_by": "TEXT",
            "updated_on": "TIMESTAMP NULL",
        }
        property_columns = {
            "prop_id": "BIGINT",
            "building_id": "BIGINT",
            "unit_name": "TEXT",
            "unit_number": "TEXT",
            "unit_type": "TEXT",
            "rms_prop": "TEXT",
            "furnishing_type": "TEXT",
            "furnish_date": "TIMESTAMP NULL",
            "rms_rent": "NUMERIC(12, 2)",
            "rms_maintain": "NUMERIC(12, 2)",
            "rms_deposit": "NUMERIC(12, 2)",
            "check_out": "TIMESTAMP NULL",
            "bookable": "BOOLEAN",
            "future_booking_days": "INTEGER",
            "active": "BOOLEAN",
            "verified": "TEXT",
            "active_search": "BOOLEAN",
            "partner_visibility": "INTEGER",
            "in_two_search": "INTEGER",
            "gd_special": "INTEGER",
            "available_from_date": "DATE",
            "listing_title": "TEXT",
            "display_property_name": "TEXT",
            "bedrooms": "INTEGER",
            "beds": "INTEGER",
            "bathrooms": "INTEGER",
            "max_guests": "INTEGER",
            "colive_type": "TEXT",
            "unit_area": "TEXT",
            "unit_age": "INTEGER",
            "facing": "TEXT",
            "prop_floor": "INTEGER",
            "prop_type_id": "INTEGER",
            "room_type_id": "INTEGER",
            "mark_check_out": "BOOLEAN",
            "mark_electricity_bill": "BOOLEAN",
            "mark_rent_paid": "BOOLEAN",
            "asset_verified": "INTEGER",
            "asset_verified_by": "TEXT",
            "asset_verified_on": "TIMESTAMP NULL",
            "flat_verified": "INTEGER",
            "flat_verified_by": "TEXT",
            "flat_verified_on": "TIMESTAMP NULL",
            "added_on": "TIMESTAMP NULL",
            "inactive_on": "TIMESTAMP NULL",
            "last_updated_by": "TEXT",
            "last_updated_on": "TIMESTAMP NULL",
            "prop_info_last_update_time": "TIMESTAMP NULL",
            "prop_info_last_updated_by": "TEXT",
            "prop_info_creation_time": "TIMESTAMP NULL",
            "rms_prop_history": "TEXT",
        }
        try:
            self.db.execute(text(f'CREATE TABLE IF NOT EXISTS "{SCHEMA_NAME}".staging_buildings (source_id BIGINT PRIMARY KEY, synced_at TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$))'))
            self.db.execute(text(f'CREATE TABLE IF NOT EXISTS "{SCHEMA_NAME}".staging_property_unit (source_id BIGINT PRIMARY KEY, synced_at TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$))'))

            for column_name, column_type in building_columns.items():
                self.db.execute(text(f'ALTER TABLE "{SCHEMA_NAME}".staging_buildings ADD COLUMN IF NOT EXISTS {column_name} {column_type}'))

            for column_name, column_type in property_columns.items():
                self.db.execute(text(f'ALTER TABLE "{SCHEMA_NAME}".staging_property_unit ADD COLUMN IF NOT EXISTS {column_name} {column_type}'))

            index_sql = [
                f'CREATE UNIQUE INDEX IF NOT EXISTS ux_staging_buildings_source_id ON "{SCHEMA_NAME}".staging_buildings (source_id)',
                f'CREATE INDEX IF NOT EXISTS idx_staging_buildings_building_id ON "{SCHEMA_NAME}".staging_buildings (building_id)',
                f'CREATE INDEX IF NOT EXISTS idx_staging_buildings_name_lower ON "{SCHEMA_NAME}".staging_buildings ((LOWER(building_name)))',
                f'CREATE INDEX IF NOT EXISTS idx_staging_buildings_staff_refs ON "{SCHEMA_NAME}".staging_buildings (caretaker, supervisor, ops_manager, sales)',
                f'CREATE UNIQUE INDEX IF NOT EXISTS ux_staging_property_unit_source_id ON "{SCHEMA_NAME}".staging_property_unit (source_id)',
                f'CREATE INDEX IF NOT EXISTS idx_staging_property_unit_prop_id ON "{SCHEMA_NAME}".staging_property_unit (prop_id)',
                f'CREATE INDEX IF NOT EXISTS idx_staging_property_unit_building_id ON "{SCHEMA_NAME}".staging_property_unit (building_id)',
                f'CREATE INDEX IF NOT EXISTS idx_staging_property_unit_avl_date ON "{SCHEMA_NAME}".staging_property_unit (available_from_date)',
                f'CREATE INDEX IF NOT EXISTS idx_staging_property_unit_unit_type ON "{SCHEMA_NAME}".staging_property_unit (unit_type)',
            ]
            for sql in index_sql:
                self.db.execute(text(sql))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _already_refreshed_today(self, checkpoint: dict) -> bool:
        updated_at = checkpoint.get("updated_at")
        if updated_at is None or checkpoint.get("last_status") != "SUCCESS":
            return False

        current_date = self.db.execute(text("SELECT CURRENT_DATE")).scalar()
        try:
            return updated_at.date() >= current_date
        except Exception:
            return str(updated_at)[:10] >= str(current_date)

    def sync_buildings(self, limit: int = 50000, mode: str = "hybrid"):
        """
        Sync building_details into AnalyticsEngine.staging_buildings.

        hybrid mode:
        - new buildings: bid > last_id
        - updated old buildings: updated_on > last_timestamp

        This is required because updated_on is NULL by default and only gets a value
        when a building is edited.
        """
        self._ensure_building_property_tables()

        table = "building_details"
        cp = self.checkpoint.get_checkpoint(table)
        last_id = cp["last_id"]
        last_ts = cp["last_timestamp"]
        normalized_mode = str(mode or "hybrid").strip().lower()

        if normalized_mode == "daily" and self._already_refreshed_today(cp):
            return {
                "inserted_or_updated": 0,
                "skipped": True,
                "reason": "building_details already refreshed today",
                "last_id": last_id,
                "last_timestamp": last_ts,
            }

        source_engine = get_thirdparty_mysql_engine()
        base_select = """
            SELECT
                bid, bname, bcity, barea, baddress, bpincode, glat, glng,
                direction, caretaker, superviser, ops_manager, finance_superviser,
                sales, sales_phone_no, marketing, status, show_tenants, rent_model,
                future_booking_plan, agreement_date, agreement_renewable_date,
                position_date, grace_period, wifi_account_id, wifi_details,
                wifi_comment, wifi_expire_on, wifi_recharge, updated_by, updated_on
            FROM building_details
        """

        id_rows = []
        time_rows = []

        if normalized_mode in {"hybrid", "id"}:
            id_rows = fetch_all(
                source_engine,
                base_select + """
                WHERE status <> 1
                AND bid > :last_id
                ORDER BY bid
                LIMIT :limit
                """,
                {"last_id": int(last_id or 0), "limit": int(limit)},
            )

        if normalized_mode in {"hybrid", "time"}:
            time_rows = fetch_all(
                source_engine,
                base_select + """
                WHERE status <> 1
                AND updated_on IS NOT NULL
                AND (
                        :last_timestamp IS NULL
                        OR updated_on > :last_timestamp
                        OR (updated_on = :last_timestamp AND bid > :last_id)
                    )
                ORDER BY updated_on, bid
                LIMIT :limit
                """,
                {
                    "last_timestamp": last_ts,
                    "last_id": int(last_id or 0),
                    "limit": int(limit),
                },
            )

        if normalized_mode in {"daily", "full", "full_daily"}:
            rows = fetch_all(
                source_engine,
                base_select + """
                WHERE status <> 1
                ORDER BY bid
                LIMIT :limit
                """,
                {"limit": int(limit)},
            )
        else:
            rows = list(id_rows) + list(time_rows)

        if not rows:
            self.checkpoint.update_success(
                table,
                last_id=last_id or 0,
                last_timestamp=last_ts,
                batch_count=0,
                notes=f"No building_details rows fetched; mode={normalized_mode}.",
            )
            return {
                "inserted_or_updated": 0,
                "last_id": last_id,
                "last_timestamp": last_ts,
            }

        payload_by_source_id = {}

        for row in rows:
            r = dict(row)
            source_id = self.safe_int(r.get("bid"))
            if source_id is None:
                continue

            sales_phone_raw = r.get("sales_phone_no")
            updated_on = self.safe_dt(r.get("updated_on"))

            payload_by_source_id[source_id] = {
                "source_id": source_id,
                "building_id": source_id,
                "building_name": self._clean_text(r.get("bname")),
                "city": self._clean_text(r.get("bcity")),
                "area": self._clean_text(r.get("barea")),
                "address": self._clean_text(r.get("baddress")),
                "pincode": self.safe_int(r.get("bpincode")),
                "glat": self.safe_numeric(r.get("glat")),
                "glng": self.safe_numeric(r.get("glng")),
                "direction_note": self._clean_text(r.get("direction")),
                "caretaker": self._clean_text(r.get("caretaker")),
                "supervisor": self._clean_text(r.get("superviser")),
                "ops_manager": self._clean_text(r.get("ops_manager")),
                "finance_supervisor": self._clean_text(r.get("finance_superviser")),
                "sales": self._clean_text(r.get("sales")),
                "sales_phone": self._clean_text(sales_phone_raw),
                "sales_normalized_phone": self.norm_phone(sales_phone_raw),
                "marketing": self._clean_text(r.get("marketing")),
                "building_status": self.safe_int(r.get("status")),
                "show_tenants": self.safe_bool(r.get("show_tenants")),
                "rent_model": self._clean_text(r.get("rent_model")),
                "future_booking_plan": self.safe_int(r.get("future_booking_plan")),
                "agreement_date": self.safe_dt(r.get("agreement_date")),
                "agreement_renewable_date": self.safe_dt(r.get("agreement_renewable_date")),
                "position_date": self.safe_dt(r.get("position_date")),
                "grace_period": self.safe_int(r.get("grace_period")),
                "wifi_account_id": self._clean_text(r.get("wifi_account_id")),
                "wifi_details": self._clean_text(r.get("wifi_details")),
                "wifi_comment": self._clean_text(r.get("wifi_comment")),
                "wifi_expire_on": self.safe_dt(r.get("wifi_expire_on")),
                "wifi_recharge": self.safe_int(r.get("wifi_recharge")),
                "updated_by": self._clean_text(r.get("updated_by")),
                "updated_on": updated_on,
                "_updated_on": updated_on,
            }

        payload = list(payload_by_source_id.values())

        if not payload:
            self.checkpoint.update_success(
                table,
                last_id=last_id or 0,
                last_timestamp=last_ts,
                batch_count=0,
                notes=f"building_details rows had no usable bid; mode={normalized_mode}.",
            )
            return {
                "inserted_or_updated": 0,
                "last_id": last_id,
                "last_timestamp": last_ts,
            }

        sql = text(
            f"""
            INSERT INTO "{SCHEMA_NAME}".staging_buildings AS target (
                source_id, building_id, building_name, city, area, address, pincode,
                glat, glng, direction_note, caretaker, supervisor, ops_manager,
                finance_supervisor, sales, sales_phone, sales_normalized_phone,
                marketing, building_status, show_tenants, rent_model,
                future_booking_plan, agreement_date, agreement_renewable_date,
                position_date, grace_period, wifi_account_id, wifi_details,
                wifi_comment, wifi_expire_on, wifi_recharge, updated_by,
                updated_on, synced_at
            ) VALUES (
                :source_id, :building_id, :building_name, :city, :area, :address, :pincode,
                :glat, :glng, :direction_note, :caretaker, :supervisor, :ops_manager,
                :finance_supervisor, :sales, :sales_phone, :sales_normalized_phone,
                :marketing, :building_status, :show_tenants, :rent_model,
                :future_booking_plan, :agreement_date, :agreement_renewable_date,
                :position_date, :grace_period, :wifi_account_id, :wifi_details,
                :wifi_comment, :wifi_expire_on, :wifi_recharge, :updated_by,
                :updated_on, (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            )
            ON CONFLICT (source_id) DO UPDATE SET
                building_id = EXCLUDED.building_id,
                building_name = EXCLUDED.building_name,
                city = EXCLUDED.city,
                area = EXCLUDED.area,
                address = EXCLUDED.address,
                pincode = EXCLUDED.pincode,
                glat = EXCLUDED.glat,
                glng = EXCLUDED.glng,
                direction_note = EXCLUDED.direction_note,
                caretaker = EXCLUDED.caretaker,
                supervisor = EXCLUDED.supervisor,
                ops_manager = EXCLUDED.ops_manager,
                finance_supervisor = EXCLUDED.finance_supervisor,
                sales = EXCLUDED.sales,
                sales_phone = EXCLUDED.sales_phone,
                sales_normalized_phone = EXCLUDED.sales_normalized_phone,
                marketing = EXCLUDED.marketing,
                building_status = EXCLUDED.building_status,
                show_tenants = EXCLUDED.show_tenants,
                rent_model = EXCLUDED.rent_model,
                future_booking_plan = EXCLUDED.future_booking_plan,
                agreement_date = EXCLUDED.agreement_date,
                agreement_renewable_date = EXCLUDED.agreement_renewable_date,
                position_date = EXCLUDED.position_date,
                grace_period = EXCLUDED.grace_period,
                wifi_account_id = EXCLUDED.wifi_account_id,
                wifi_details = EXCLUDED.wifi_details,
                wifi_comment = EXCLUDED.wifi_comment,
                wifi_expire_on = EXCLUDED.wifi_expire_on,
                wifi_recharge = EXCLUDED.wifi_recharge,
                updated_by = EXCLUDED.updated_by,
                updated_on = EXCLUDED.updated_on,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            WHERE target.building_id IS DISTINCT FROM EXCLUDED.building_id
            OR target.building_name IS DISTINCT FROM EXCLUDED.building_name
            OR target.city IS DISTINCT FROM EXCLUDED.city
            OR target.area IS DISTINCT FROM EXCLUDED.area
            OR target.address IS DISTINCT FROM EXCLUDED.address
            OR target.pincode IS DISTINCT FROM EXCLUDED.pincode
            OR target.glat IS DISTINCT FROM EXCLUDED.glat
            OR target.glng IS DISTINCT FROM EXCLUDED.glng
            OR target.direction_note IS DISTINCT FROM EXCLUDED.direction_note
            OR target.caretaker IS DISTINCT FROM EXCLUDED.caretaker
            OR target.supervisor IS DISTINCT FROM EXCLUDED.supervisor
            OR target.ops_manager IS DISTINCT FROM EXCLUDED.ops_manager
            OR target.finance_supervisor IS DISTINCT FROM EXCLUDED.finance_supervisor
            OR target.sales IS DISTINCT FROM EXCLUDED.sales
            OR target.sales_phone IS DISTINCT FROM EXCLUDED.sales_phone
            OR target.sales_normalized_phone IS DISTINCT FROM EXCLUDED.sales_normalized_phone
            OR target.marketing IS DISTINCT FROM EXCLUDED.marketing
            OR target.building_status IS DISTINCT FROM EXCLUDED.building_status
            OR target.show_tenants IS DISTINCT FROM EXCLUDED.show_tenants
            OR target.rent_model IS DISTINCT FROM EXCLUDED.rent_model
            OR target.future_booking_plan IS DISTINCT FROM EXCLUDED.future_booking_plan
            OR target.agreement_date IS DISTINCT FROM EXCLUDED.agreement_date
            OR target.agreement_renewable_date IS DISTINCT FROM EXCLUDED.agreement_renewable_date
            OR target.position_date IS DISTINCT FROM EXCLUDED.position_date
            OR target.grace_period IS DISTINCT FROM EXCLUDED.grace_period
            OR target.wifi_account_id IS DISTINCT FROM EXCLUDED.wifi_account_id
            OR target.wifi_details IS DISTINCT FROM EXCLUDED.wifi_details
            OR target.wifi_comment IS DISTINCT FROM EXCLUDED.wifi_comment
            OR target.wifi_expire_on IS DISTINCT FROM EXCLUDED.wifi_expire_on
            OR target.wifi_recharge IS DISTINCT FROM EXCLUDED.wifi_recharge
            OR target.updated_by IS DISTINCT FROM EXCLUDED.updated_by
            OR target.updated_on IS DISTINCT FROM EXCLUDED.updated_on
            """
        )

        self._bulk_upsert_in_chunks(sql, payload, 5000)

        id_cursor_ids = [
            self.safe_int(dict(row).get("bid"))
            for row in id_rows
            if self.safe_int(dict(row).get("bid")) is not None
        ]

        if normalized_mode == "hybrid":
            new_last_id = max([int(last_id or 0), *id_cursor_ids], default=int(last_id or 0))
        else:
            new_last_id = max(
                (r["source_id"] for r in payload if r.get("source_id") is not None),
                default=last_id or 0,
            )

        new_last_ts = max(
            (r["_updated_on"] for r in payload if r.get("_updated_on") is not None),
            default=last_ts,
        )

        self.checkpoint.update_success(
            table,
            last_id=new_last_id,
            last_timestamp=new_last_ts,
            batch_count=len(payload),
            notes=(
                f"building master refresh; mode={normalized_mode}; "
                f"id_rows={len(id_rows)}; updated_rows={len(time_rows)}; "
                f"deduped_payload={len(payload)}"
            ),
        )

        return {
            "inserted_or_updated": len(payload),
            "last_id": new_last_id,
            "last_timestamp": new_last_ts,
        }
    def sync_property_units(self, limit: int = 100000, mode: str = "daily"):
        """
        Sync RMS property units from prop_tracking + selected prop_info fields.

        Address/location columns are intentionally not synced here. Property
        address should be read through staging_buildings using building_id.
        """
        self._ensure_building_property_tables()

        table = "property_unit"
        cp = self.checkpoint.get_checkpoint(table)
        last_id = cp["last_id"]
        last_ts = cp["last_timestamp"]
        normalized_mode = str(mode or "daily").strip().lower()

        if normalized_mode == "daily" and self._already_refreshed_today(cp):
            return {
                "inserted_or_updated": 0,
                "skipped": True,
                "reason": "property_unit already refreshed today",
                "last_id": last_id,
                "last_timestamp": last_ts,
            }

        source_engine = get_thirdparty_mysql_engine()
        base_select = """
            SELECT
                pt.id, pt.prop_id, pt.building_id, pt.name, pt.unit, pt.unit_type,
                pt.rms_prop, pt.furnishing_type, pt.furnish_date, pt.rms_rent,
                pt.rms_maintain, pt.rms_deposit, pt.check_out, pt.mark_check_out,
                pt.mark_electricity_bill, pt.mark_rent_paid, pt.asset_verified,
                pt.asset_verified_by, pt.asset_verified_on, pt.flat_verified,
                pt.flat_verified_by, pt.flat_verified_on, pt.added_on, pt.inactive_on,
                pt.last_updated_by, pt.last_updated_on, pt.rms_prop_history,
                pi.bookable, pi.future_booking_days, pi.active, pi.verified,
                pi.active_search, pi.partner_visibility, pi.in_two_search, pi.gd_special,
                pi.avl_date, pi.title, pi.property_name, pi.bedrooms, pi.beds,
                pi.bathrooms, pi.max_guests, pi.colive_type, pi.unit_area, pi.unit_age,
                pi.facing, pi.prop_floor, pi.prop_type_id, pi.room_type_id,
                pi.last_update_time AS prop_info_last_update_time,
                pi.last_updated_by AS prop_info_last_updated_by,
                pi.creation_time AS prop_info_creation_time
            FROM prop_tracking pt
            LEFT JOIN prop_info pi ON pi.prop_id = pt.prop_id
            WHERE pt.rms_prop = 'RMS Prop'
        """

        if normalized_mode == "time":
            rows = fetch_all(
                source_engine,
                base_select + """
                  AND COALESCE(pt.last_updated_on, pi.last_update_time, pt.added_on) IS NOT NULL
                  AND (
                        :last_timestamp IS NULL
                        OR COALESCE(pt.last_updated_on, pi.last_update_time, pt.added_on) > :last_timestamp
                        OR (
                            COALESCE(pt.last_updated_on, pi.last_update_time, pt.added_on) = :last_timestamp
                            AND pt.id > :last_id
                        )
                      )
                ORDER BY COALESCE(pt.last_updated_on, pi.last_update_time, pt.added_on), pt.id
                LIMIT :limit
                """,
                {"last_timestamp": last_ts, "last_id": int(last_id or 0), "limit": int(limit)},
            )
        elif normalized_mode == "id":
            rows = fetch_all(
                source_engine,
                base_select + """
                  AND pt.id > :last_id
                ORDER BY pt.id
                LIMIT :limit
                """,
                {"last_id": int(last_id or 0), "limit": int(limit)},
            )
        else:
            rows = fetch_all(
                source_engine,
                base_select + """
                ORDER BY pt.id
                LIMIT :limit
                """,
                {"limit": int(limit)},
            )

        if not rows:
            self.checkpoint.update_success(
                table,
                last_id=last_id or 0,
                last_timestamp=last_ts,
                batch_count=0,
                notes=f"No RMS property units fetched; mode={normalized_mode}.",
            )
            return {"inserted_or_updated": 0, "last_id": last_id, "last_timestamp": last_ts}

        payload = []
        seen_source_ids = set()
        for row in rows:
            r = dict(row)
            source_id = self.safe_int(r.get("id"))
            if source_id is None or source_id in seen_source_ids:
                continue
            seen_source_ids.add(source_id)
            payload.append(
                {
                    "source_id": source_id,
                    "prop_id": self.safe_int(r.get("prop_id")),
                    "building_id": self.safe_int(r.get("building_id")),
                    "unit_name": self._clean_text(r.get("name")),
                    "unit_number": self._clean_text(r.get("unit")),
                    "unit_type": self._clean_text(r.get("unit_type")),
                    "rms_prop": self._clean_text(r.get("rms_prop")),
                    "furnishing_type": self._clean_text(r.get("furnishing_type")),
                    "furnish_date": self.safe_dt(r.get("furnish_date")),
                    "rms_rent": self.safe_numeric(r.get("rms_rent")),
                    "rms_maintain": self.safe_numeric(r.get("rms_maintain")),
                    "rms_deposit": self.safe_numeric(r.get("rms_deposit")),
                    "check_out": self.safe_dt(r.get("check_out")),
                    "bookable": self.safe_bool(r.get("bookable")),
                    "future_booking_days": self.safe_int(r.get("future_booking_days")),
                    "active": self.safe_bool(r.get("active")),
                    "verified": self._clean_text(r.get("verified")),
                    "active_search": self.safe_bool(r.get("active_search")),
                    "partner_visibility": self.safe_int(r.get("partner_visibility")),
                    "in_two_search": self.safe_int(r.get("in_two_search")),
                    "gd_special": self.safe_int(r.get("gd_special")),
                    "available_from_date": self.safe_dt(r.get("avl_date")),
                    "listing_title": self._clean_text(r.get("title")),
                    "display_property_name": self._clean_text(r.get("property_name")),
                    "bedrooms": self.safe_int(r.get("bedrooms")),
                    "beds": self.safe_int(r.get("beds")),
                    "bathrooms": self.safe_int(r.get("bathrooms")),
                    "max_guests": self.safe_int(r.get("max_guests")),
                    "colive_type": self._clean_text(r.get("colive_type")),
                    "unit_area": self._clean_text(r.get("unit_area")),
                    "unit_age": self.safe_int(r.get("unit_age")),
                    "facing": self._clean_text(r.get("facing")),
                    "prop_floor": self.safe_int(r.get("prop_floor")),
                    "prop_type_id": self.safe_int(r.get("prop_type_id")),
                    "room_type_id": self.safe_int(r.get("room_type_id")),
                    "mark_check_out": self.safe_bool(r.get("mark_check_out")),
                    "mark_electricity_bill": self.safe_bool(r.get("mark_electricity_bill")),
                    "mark_rent_paid": self.safe_bool(r.get("mark_rent_paid")),
                    "asset_verified": self.safe_int(r.get("asset_verified")),
                    "asset_verified_by": self._clean_text(r.get("asset_verified_by")),
                    "asset_verified_on": self.safe_dt(r.get("asset_verified_on")),
                    "flat_verified": self.safe_int(r.get("flat_verified")),
                    "flat_verified_by": self._clean_text(r.get("flat_verified_by")),
                    "flat_verified_on": self.safe_dt(r.get("flat_verified_on")),
                    "added_on": self.safe_dt(r.get("added_on")),
                    "inactive_on": self.safe_dt(r.get("inactive_on")),
                    "last_updated_by": self._clean_text(r.get("last_updated_by")),
                    "last_updated_on": self.safe_dt(r.get("last_updated_on")),
                    "prop_info_last_update_time": self.safe_dt(r.get("prop_info_last_update_time")),
                    "prop_info_last_updated_by": self._clean_text(r.get("prop_info_last_updated_by")),
                    "prop_info_creation_time": self.safe_dt(r.get("prop_info_creation_time")),
                    "rms_prop_history": self._clean_text(r.get("rms_prop_history")),
                    "_ts": self.safe_dt(
                        r.get("last_updated_on")
                        or r.get("prop_info_last_update_time")
                        or r.get("added_on")
                    ),
                }
            )

        if not payload:
            self.checkpoint.update_success(
                table,
                last_id=last_id or 0,
                last_timestamp=last_ts,
                batch_count=0,
                notes=f"property_unit rows had no usable id; mode={normalized_mode}.",
            )
            return {"inserted_or_updated": 0, "last_id": last_id, "last_timestamp": last_ts}

        sql = text(
            f"""
            INSERT INTO "{SCHEMA_NAME}".staging_property_unit (
                source_id, prop_id, building_id, unit_name, unit_number, unit_type,
                rms_prop, furnishing_type, furnish_date, rms_rent, rms_maintain,
                rms_deposit, check_out, bookable, future_booking_days, active,
                verified, active_search, partner_visibility, in_two_search,
                gd_special, available_from_date, listing_title, display_property_name,
                bedrooms, beds, bathrooms, max_guests, colive_type, unit_area,
                unit_age, facing, prop_floor, prop_type_id, room_type_id,
                mark_check_out, mark_electricity_bill, mark_rent_paid,
                asset_verified, asset_verified_by, asset_verified_on,
                flat_verified, flat_verified_by, flat_verified_on, added_on,
                inactive_on, last_updated_by, last_updated_on,
                prop_info_last_update_time, prop_info_last_updated_by,
                prop_info_creation_time, rms_prop_history, synced_at
            ) VALUES (
                :source_id, :prop_id, :building_id, :unit_name, :unit_number, :unit_type,
                :rms_prop, :furnishing_type, :furnish_date, :rms_rent, :rms_maintain,
                :rms_deposit, :check_out, :bookable, :future_booking_days, :active,
                :verified, :active_search, :partner_visibility, :in_two_search,
                :gd_special, :available_from_date, :listing_title, :display_property_name,
                :bedrooms, :beds, :bathrooms, :max_guests, :colive_type, :unit_area,
                :unit_age, :facing, :prop_floor, :prop_type_id, :room_type_id,
                :mark_check_out, :mark_electricity_bill, :mark_rent_paid,
                :asset_verified, :asset_verified_by, :asset_verified_on,
                :flat_verified, :flat_verified_by, :flat_verified_on, :added_on,
                :inactive_on, :last_updated_by, :last_updated_on,
                :prop_info_last_update_time, :prop_info_last_updated_by,
                :prop_info_creation_time, :rms_prop_history, (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            )
            ON CONFLICT (source_id) DO UPDATE SET
                prop_id = EXCLUDED.prop_id,
                building_id = EXCLUDED.building_id,
                unit_name = EXCLUDED.unit_name,
                unit_number = EXCLUDED.unit_number,
                unit_type = EXCLUDED.unit_type,
                rms_prop = EXCLUDED.rms_prop,
                furnishing_type = EXCLUDED.furnishing_type,
                furnish_date = EXCLUDED.furnish_date,
                rms_rent = EXCLUDED.rms_rent,
                rms_maintain = EXCLUDED.rms_maintain,
                rms_deposit = EXCLUDED.rms_deposit,
                check_out = EXCLUDED.check_out,
                bookable = EXCLUDED.bookable,
                future_booking_days = EXCLUDED.future_booking_days,
                active = EXCLUDED.active,
                verified = EXCLUDED.verified,
                active_search = EXCLUDED.active_search,
                partner_visibility = EXCLUDED.partner_visibility,
                in_two_search = EXCLUDED.in_two_search,
                gd_special = EXCLUDED.gd_special,
                available_from_date = EXCLUDED.available_from_date,
                listing_title = EXCLUDED.listing_title,
                display_property_name = EXCLUDED.display_property_name,
                bedrooms = EXCLUDED.bedrooms,
                beds = EXCLUDED.beds,
                bathrooms = EXCLUDED.bathrooms,
                max_guests = EXCLUDED.max_guests,
                colive_type = EXCLUDED.colive_type,
                unit_area = EXCLUDED.unit_area,
                unit_age = EXCLUDED.unit_age,
                facing = EXCLUDED.facing,
                prop_floor = EXCLUDED.prop_floor,
                prop_type_id = EXCLUDED.prop_type_id,
                room_type_id = EXCLUDED.room_type_id,
                mark_check_out = EXCLUDED.mark_check_out,
                mark_electricity_bill = EXCLUDED.mark_electricity_bill,
                mark_rent_paid = EXCLUDED.mark_rent_paid,
                asset_verified = EXCLUDED.asset_verified,
                asset_verified_by = EXCLUDED.asset_verified_by,
                asset_verified_on = EXCLUDED.asset_verified_on,
                flat_verified = EXCLUDED.flat_verified,
                flat_verified_by = EXCLUDED.flat_verified_by,
                flat_verified_on = EXCLUDED.flat_verified_on,
                added_on = EXCLUDED.added_on,
                inactive_on = EXCLUDED.inactive_on,
                last_updated_by = EXCLUDED.last_updated_by,
                last_updated_on = EXCLUDED.last_updated_on,
                prop_info_last_update_time = EXCLUDED.prop_info_last_update_time,
                prop_info_last_updated_by = EXCLUDED.prop_info_last_updated_by,
                prop_info_creation_time = EXCLUDED.prop_info_creation_time,
                rms_prop_history = EXCLUDED.rms_prop_history,
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
            notes=f"RMS property unit refresh; mode={normalized_mode}; source_rows={len(rows)}",
        )
        return {"inserted_or_updated": len(payload), "last_id": new_last_id, "last_timestamp": new_last_ts}

    def sync_user_accounts(self, limit: int = 50000, mode: str = "id"):
        table = "user_account"
        cp = self.checkpoint.get_checkpoint(table)
        last_id = cp["last_id"]
        last_ts = cp["last_timestamp"]
        source_engine = get_thirdparty_mysql_engine()

        if mode == "time":
            rows = fetch_all(
                source_engine,
                """
                SELECT
                    a.id,
                    a.username,
                    a.email,
                    a.contact_no,
                    a.is_admin,
                    a.team,
                    a.active,
                    a.createdon,
                    a.last_login_time,
                    d.contact_num
                FROM a3m_account a
                LEFT JOIN a3m_account_details d ON d.account_id = a.id
                WHERE a.createdon IS NOT NULL
                  AND (
                        :last_timestamp IS NULL
                        OR a.createdon > :last_timestamp
                        OR (a.createdon = :last_timestamp AND a.id > :last_id)
                      )
                ORDER BY a.createdon, a.id
                LIMIT :limit
                """,
                {"last_timestamp": last_ts, "last_id": int(last_id or 0), "limit": int(limit)},
            )
        else:
            rows = fetch_all(
                source_engine,
                """
                SELECT
                    a.id,
                    a.username,
                    a.email,
                    a.contact_no,
                    a.is_admin,
                    a.team,
                    a.active,
                    a.createdon,
                    a.last_login_time,
                    d.contact_num
                FROM a3m_account a
                LEFT JOIN a3m_account_details d ON d.account_id = a.id
                WHERE a.id > :last_id
                ORDER BY a.id
                LIMIT :limit
                """,
                {"last_id": int(last_id or 0), "limit": int(limit)},
            )

        if not rows:
            return {"inserted_or_updated": 0, "last_id": last_id, "last_timestamp": last_ts}

        payload = []
        for row in rows:
            r = dict(row)
            phone_raw = r.get("contact_num") or r.get("contact_no")
            payload.append(
                {
                    "source_id": self.safe_int(r.get("id")),
                    "username": self._clean_text(r.get("username")),
                    "email": self._clean_lower_email(r.get("email")),
                    "phone_number": self._clean_text(phone_raw),
                    "normalized_phone": self.norm_phone(phone_raw),
                    "is_admin": self._clean_text(r.get("is_admin")),
                    "team": self._clean_team(r.get("team")),
                    "active": self._clean_text(r.get("active")),
                    "created_on": self.safe_dt(r.get("createdon")),
                    "last_login_time": self.safe_dt(r.get("last_login_time")),
                }
            )

        sql = text(
            f"""
            INSERT INTO "{SCHEMA_NAME}".staging_user_account (
                source_id, username, email, phone_number, normalized_phone,
                is_admin, team, active, created_on, last_login_time, synced_at
            ) VALUES (
                :source_id, :username, :email, :phone_number, :normalized_phone,
                :is_admin, :team, :active, :created_on, :last_login_time, (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            )
            ON CONFLICT (source_id) DO UPDATE SET
                username = EXCLUDED.username,
                email = EXCLUDED.email,
                phone_number = EXCLUDED.phone_number,
                normalized_phone = EXCLUDED.normalized_phone,
                is_admin = EXCLUDED.is_admin,
                team = EXCLUDED.team,
                active = EXCLUDED.active,
                created_on = EXCLUDED.created_on,
                last_login_time = EXCLUDED.last_login_time,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            """
        )

        self._bulk_upsert_in_chunks(sql, payload, 5000)
        new_last_id = max(r["source_id"] for r in payload if r.get("source_id") is not None)
        new_last_ts = max((r["created_on"] for r in payload if r.get("created_on") is not None), default=last_ts)
        self.checkpoint.update_success(table, last_id=new_last_id, last_timestamp=new_last_ts, batch_count=len(payload))
        return {"inserted_or_updated": len(payload), "last_id": new_last_id, "last_timestamp": new_last_ts}

    def sync_leads(self, limit: int = 30000, mode: str = "id"):
        table = "lead_tracking"
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
                    booking_id,
                    user_id,
                    email_id,
                    priority,
                    assign_to,
                    added_by,
                    generated_by,
                    origin,
                    added_on,
                    closed_on,
                    status,
                    contact_details,
                    contact_details2
                FROM lead_tracking
                WHERE added_on IS NOT NULL
                  AND (
                        :last_timestamp IS NULL
                        OR added_on > :last_timestamp
                        OR (added_on = :last_timestamp AND id > :last_id)
                      )
                ORDER BY added_on, id
                LIMIT :limit
                """,
                {"last_timestamp": last_ts, "last_id": int(last_id or 0), "limit": int(limit)},
            )
        else:
            rows = fetch_all(
                source_engine,
                """
                SELECT
                    id,
                    booking_id,
                    user_id,
                    email_id,
                    priority,
                    assign_to,
                    added_by,
                    generated_by,
                    origin,
                    added_on,
                    closed_on,
                    status,
                    contact_details,
                    contact_details2
                FROM lead_tracking
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
            assigned_to = self._clean_text(r.get("assign_to"))
            payload.append(
                {
                    "source_id": self.safe_int(r.get("id")),
                    "user_id": self.safe_int(r.get("user_id")),
                    "booking_id": self.safe_int(r.get("booking_id")),
                    "executive_id": assigned_to,
                    "created_at": self.safe_dt(r.get("added_on")),
                    "closed_at": self.safe_dt(r.get("closed_on")),
                    "raw_status": self._clean_text(r.get("status")),
                    "email": self._clean_lower_email(r.get("email_id")),
                    "priority": r.get("priority"),
                    "added_by": self._clean_text(r.get("added_by")),
                    "assigned_to": assigned_to,
                    "generated_by": self._clean_text(r.get("generated_by")),
                    "origin": self._clean_text(r.get("origin")),
                    "contact_number": self.norm_phone(r.get("contact_details")),
                    "contact_number_alt": self.norm_phone(r.get("contact_details2")),
                }
            )

        sql = text(
            f"""
            INSERT INTO "{SCHEMA_NAME}".staging_lead_tracking (
                source_id, user_id, booking_id, executive_id, created_at, closed_at,
                raw_status, email, priority, added_by, assigned_to,
                generated_by, origin, contact_number, contact_number_alt, synced_at
            ) VALUES (
                :source_id, :user_id, :booking_id, :executive_id, :created_at, :closed_at,
                :raw_status, :email, :priority, :added_by, :assigned_to,
                :generated_by, :origin, :contact_number, :contact_number_alt, (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            )
            ON CONFLICT (source_id) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                booking_id = EXCLUDED.booking_id,
                executive_id = EXCLUDED.executive_id,
                created_at = EXCLUDED.created_at,
                closed_at = EXCLUDED.closed_at,
                raw_status = EXCLUDED.raw_status,
                email = EXCLUDED.email,
                priority = EXCLUDED.priority,
                added_by = EXCLUDED.added_by,
                assigned_to = EXCLUDED.assigned_to,
                generated_by = EXCLUDED.generated_by,
                origin = EXCLUDED.origin,
                contact_number = EXCLUDED.contact_number,
                contact_number_alt = EXCLUDED.contact_number_alt,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            """
        )

        self._bulk_upsert_in_chunks(sql, payload, 5000)
        new_last_id = max(r["source_id"] for r in payload if r.get("source_id") is not None)
        new_last_ts = max((r["created_at"] for r in payload if r.get("created_at") is not None), default=last_ts)
        self.checkpoint.update_success(table, last_id=new_last_id, last_timestamp=new_last_ts, batch_count=len(payload))
        return {"inserted_or_updated": len(payload), "last_id": new_last_id, "last_timestamp": new_last_ts}

    def sync_call_recordings_transcript(self, limit: int = 50000, mode: str = "id"):
        table = "call_recordings_transcript"
        cp = self.checkpoint.get_checkpoint(table)
        last_id = cp["last_id"]
        last_ts = cp["last_timestamp"]
        source_last_ts = self.source_utc_cursor_from_ist(last_ts)
        source_engine = get_thirdparty_pg_engine()

        select_columns = """
            id, emp_phone_number, emp_name, customer_phone_number, call_datetime,
            call_duration, call_type, department, audio_url, transcript_text,
            transcript_text_eleven_labs, raw_eleven_labs_transcript, filename,
            uploaded_at, status, call_id, intent, emotion, tone, action_layer,
            context, outcome, language, priority, sync_status, raw_transcripts,
            translated_text
        """

        if mode == "time":
            rows = fetch_all(
                source_engine,
                f"""
                SELECT {select_columns}
                FROM public.call_recordings_transcript
                WHERE COALESCE(call_datetime, uploaded_at) IS NOT NULL
                  AND COALESCE("_PEERDB_IS_DELETED", false) = false
                  AND (
                        :last_timestamp IS NULL
                        OR COALESCE(call_datetime, uploaded_at) > :last_timestamp
                        OR (COALESCE(call_datetime, uploaded_at) = :last_timestamp AND id > :last_id)
                      )
                ORDER BY COALESCE(call_datetime, uploaded_at), id
                LIMIT :limit
                """,
                {"last_timestamp": source_last_ts, "last_id": int(last_id or 0), "limit": int(limit)},
            )
        else:
            rows = fetch_all(
                source_engine,
                f"""
                SELECT {select_columns}
                FROM public.call_recordings_transcript
                WHERE id > :last_id
                  AND COALESCE("_PEERDB_IS_DELETED", false) = false
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
            duration = self.safe_int(r.get("call_duration")) or 0
            raw_eleven_labs = r.get("raw_eleven_labs_transcript")
            if raw_eleven_labs is not None and not isinstance(raw_eleven_labs, str):
                raw_eleven_labs = json.dumps(raw_eleven_labs, default=str, ensure_ascii=False)
            payload.append({
                "source_id": self.safe_int(r.get("id")),
                "lead_id": None,
                "executive_id": self._clean_text(r.get("emp_phone_number") or r.get("emp_name")),
                "executive_name": self._clean_text(r.get("emp_name")),
                "call_time": self.safe_dt_from_utc(r.get("call_datetime") or r.get("uploaded_at")),
                "talk_time_sec": duration,
                "call_direction": self._normalize_call_direction(r.get("call_type")),
                "call_result": self._derive_call_result(duration),
                "counterparty_phone": self.norm_phone(r.get("customer_phone_number")),
                "sales_phone": self.norm_phone(r.get("emp_phone_number")),
                "department": self._clean_text(r.get("department")),
                "audio_url": self._clean_text(r.get("audio_url")),
                "transcript_text": self._clean_text(r.get("transcript_text")),
                "transcript_text_eleven_labs": self._clean_text(r.get("transcript_text_eleven_labs")),
                "translated_text": self._clean_text(r.get("translated_text")),
                "raw_transcripts": self._clean_text(r.get("raw_transcripts")),
                "raw_eleven_labs_transcript": raw_eleven_labs,
                "intent": self._clean_text(r.get("intent")),
                "emotion": self._clean_text(r.get("emotion")),
                "tone": self._clean_text(r.get("tone")),
                "action_layer": self._clean_text(r.get("action_layer")),
                "context": self._clean_text(r.get("context")),
                "outcome": self._clean_text(r.get("outcome")),
                "language": self._clean_text(r.get("language")),
                "priority": self.safe_int(r.get("priority")),
                "source_call_id": self._clean_text(r.get("call_id")),
                "filename": self._clean_text(r.get("filename")),
                "uploaded_at": self.safe_dt_from_utc(r.get("uploaded_at")),
                "source_status": self.safe_int(r.get("status")),
                "sync_status": self.safe_int(r.get("sync_status")),
                "_ts": self.safe_dt_from_utc(r.get("call_datetime") or r.get("uploaded_at")),
            })

        sql = text(f"""
            INSERT INTO "{SCHEMA_NAME}".staging_call_recordings_transcript (
                source_id, lead_id, executive_id, executive_name,
                call_time, talk_time_sec, call_direction, call_result,
                counterparty_phone, sales_phone, department, audio_url,
                transcript_text, transcript_text_eleven_labs, translated_text,
                raw_transcripts, raw_eleven_labs_transcript, intent, emotion, tone,
                action_layer, context, outcome, language, priority, source_call_id,
                filename, uploaded_at, source_status, sync_status, synced_at
            ) VALUES (
                :source_id, :lead_id, :executive_id, :executive_name,
                :call_time, :talk_time_sec, :call_direction, :call_result,
                :counterparty_phone, :sales_phone, :department, :audio_url,
                :transcript_text, :transcript_text_eleven_labs, :translated_text,
                :raw_transcripts, CAST(:raw_eleven_labs_transcript AS JSONB), :intent, :emotion, :tone,
                :action_layer, :context, :outcome, :language, :priority, :source_call_id,
                :filename, :uploaded_at, :source_status, :sync_status, (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            )
            ON CONFLICT (source_id) DO UPDATE SET
                lead_id = EXCLUDED.lead_id,
                executive_id = EXCLUDED.executive_id,
                executive_name = EXCLUDED.executive_name,
                call_time = EXCLUDED.call_time,
                talk_time_sec = EXCLUDED.talk_time_sec,
                call_direction = EXCLUDED.call_direction,
                call_result = EXCLUDED.call_result,
                counterparty_phone = EXCLUDED.counterparty_phone,
                sales_phone = EXCLUDED.sales_phone,
                department = EXCLUDED.department,
                audio_url = EXCLUDED.audio_url,
                transcript_text = EXCLUDED.transcript_text,
                transcript_text_eleven_labs = EXCLUDED.transcript_text_eleven_labs,
                translated_text = EXCLUDED.translated_text,
                raw_transcripts = EXCLUDED.raw_transcripts,
                raw_eleven_labs_transcript = EXCLUDED.raw_eleven_labs_transcript,
                intent = EXCLUDED.intent,
                emotion = EXCLUDED.emotion,
                tone = EXCLUDED.tone,
                action_layer = EXCLUDED.action_layer,
                context = EXCLUDED.context,
                outcome = EXCLUDED.outcome,
                language = EXCLUDED.language,
                priority = EXCLUDED.priority,
                source_call_id = EXCLUDED.source_call_id,
                filename = EXCLUDED.filename,
                uploaded_at = EXCLUDED.uploaded_at,
                source_status = EXCLUDED.source_status,
                sync_status = EXCLUDED.sync_status,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            """)
        self._bulk_upsert_in_chunks(sql, payload, 5000)
        new_last_id = max(r["source_id"] for r in payload if r.get("source_id") is not None)
        new_last_ts = max((r["_ts"] for r in payload if r.get("_ts") is not None), default=last_ts)
        self.checkpoint.update_success(table, last_id=new_last_id, last_timestamp=new_last_ts, batch_count=len(payload))
        return {"inserted_or_updated": len(payload), "last_id": new_last_id, "last_timestamp": new_last_ts}

    def sync_call_logs(self, limit: int = 50000, mode: str = "id"):
        # Backward-compatible alias. New sync name is call_recordings_transcript.
        return self.sync_call_recordings_transcript(limit=limit, mode=mode)

    def _ensure_staff_phone_assignment_table(self):
        """Ensure staging table for MobileTagging office/pool phone-line assignments."""
        try:
            self.db.execute(text(f"""
                CREATE TABLE IF NOT EXISTS "{SCHEMA_NAME}".staging_staff_phone_assignment (
                    phone10 TEXT PRIMARY KEY,
                    phone TEXT,
                    normalized_phone TEXT,
                    tag_to TEXT,
                    city TEXT,
                    team TEXT,
                    login_status INTEGER,
                    username TEXT,
                    auto_lead BOOLEAN,
                    assign_lead BOOLEAN,
                    wa_priority BOOLEAN,
                    last_update_time TIMESTAMP NULL,
                    synced_at TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
                )
            """))
            index_sql = [
                f'CREATE INDEX IF NOT EXISTS idx_staff_phone_assignment_phone ON "{SCHEMA_NAME}".staging_staff_phone_assignment (phone)',
                f'CREATE INDEX IF NOT EXISTS idx_staff_phone_assignment_normalized_phone ON "{SCHEMA_NAME}".staging_staff_phone_assignment (normalized_phone)',
                f'CREATE INDEX IF NOT EXISTS idx_staff_phone_assignment_username ON "{SCHEMA_NAME}".staging_staff_phone_assignment (LOWER(username))',
                f'CREATE INDEX IF NOT EXISTS idx_staff_phone_assignment_tag_to ON "{SCHEMA_NAME}".staging_staff_phone_assignment (LOWER(tag_to))',
                f'CREATE INDEX IF NOT EXISTS idx_staff_phone_assignment_team ON "{SCHEMA_NAME}".staging_staff_phone_assignment (LOWER(team))',
            ]
            for sql in index_sql:
                self.db.execute(text(sql))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def sync_mobile_tagging(self, limit: int = 5000, mode: str = "daily"):
        """
        Daily refresh for MobileTagging office/pool phone-line assignments.

        Source:
          MySQL MobileTagging(Phone, tagTo, city, Team, login_status,
          username, auto_lead, assign_lead, wa_priority, last_update_time)

        Target:
          AnalyticsEngine.staging_staff_phone_assignment

        Important:
          This is NOT a staff master. It describes the current assignment or
          owner of a phone/SIM/office line. Staff activity attribution should
          prefer call-log executive_id/executive_name and use this only for
          line metadata/fallback when actor fields are missing.
        """
        self._ensure_staff_phone_assignment_table()

        table = "mobile_tagging"
        cp = self.checkpoint.get_checkpoint(table)
        normalized_mode = str(mode or "daily").strip().lower()

        if normalized_mode == "daily" and self._already_refreshed_today(cp):
            return {
                "inserted_or_updated": 0,
                "skipped": True,
                "reason": "mobile_tagging already refreshed today",
                "last_id": cp.get("last_id"),
                "last_timestamp": cp.get("last_timestamp"),
            }

        source_engine = get_thirdparty_mysql_engine()
        rows = fetch_all(
            source_engine,
            """
            SELECT
                Phone,
                tagTo,
                city,
                Team,
                login_status,
                username,
                auto_lead,
                assign_lead,
                wa_priority,
                last_update_time
            FROM MobileTagging
            WHERE Phone IS NOT NULL
              AND TRIM(CAST(Phone AS CHAR)) <> ''
            ORDER BY Phone
            LIMIT :limit
            """,
            {"limit": int(limit or 5000)},
        )

        if not rows:
            self.checkpoint.update_success(
                table,
                last_id=cp.get("last_id") or 0,
                last_timestamp=cp.get("last_timestamp"),
                batch_count=0,
                notes="No MobileTagging rows fetched.",
            )
            return {
                "inserted_or_updated": 0,
                "staff_phone_assignments_refreshed": 0,
                "last_id": cp.get("last_id") or 0,
                "last_timestamp": cp.get("last_timestamp"),
            }

        payload_by_phone10 = {}
        for row in rows:
            r = dict(row)
            raw_phone = self._clean_text(r.get("Phone"))
            normalized_phone = self.norm_phone(raw_phone)
            phone10 = None
            if normalized_phone:
                digits = "".join(ch for ch in str(normalized_phone) if ch.isdigit())
                phone10 = digits[-10:] if len(digits) >= 10 else None
            if not phone10:
                continue

            item = {
                "phone10": phone10,
                "phone": raw_phone,
                "normalized_phone": normalized_phone,
                "tag_to": self._clean_text(r.get("tagTo")),
                "city": self._clean_text(r.get("city")),
                "team": self._clean_team(r.get("Team")),
                "login_status": self.safe_int(r.get("login_status")),
                "username": self._clean_text(r.get("username")),
                "auto_lead": self.safe_bool(r.get("auto_lead")),
                "assign_lead": self.safe_bool(r.get("assign_lead")),
                "wa_priority": self.safe_bool(r.get("wa_priority")),
                "last_update_time": self.safe_dt(r.get("last_update_time")),
            }

            existing = payload_by_phone10.get(phone10)
            if existing is None:
                payload_by_phone10[phone10] = item
                continue

            current_ts = item.get("last_update_time")
            existing_ts = existing.get("last_update_time")
            if existing_ts is None or (current_ts is not None and str(current_ts) > str(existing_ts)):
                payload_by_phone10[phone10] = item

        payload = list(payload_by_phone10.values())
        if not payload:
            self.checkpoint.update_success(
                table,
                last_id=cp.get("last_id") or 0,
                last_timestamp=cp.get("last_timestamp"),
                batch_count=0,
                notes="MobileTagging rows had no usable phone10 values.",
            )
            return {
                "inserted_or_updated": 0,
                "staff_phone_assignments_refreshed": 0,
                "last_id": cp.get("last_id") or 0,
                "last_timestamp": cp.get("last_timestamp"),
            }

        sql = text(
            f"""
            INSERT INTO "{SCHEMA_NAME}".staging_staff_phone_assignment (
                phone10, phone, normalized_phone, tag_to, city, team,
                login_status, username, auto_lead, assign_lead, wa_priority,
                last_update_time, synced_at
            ) VALUES (
                :phone10, :phone, :normalized_phone, :tag_to, :city, :team,
                :login_status, :username, :auto_lead, :assign_lead, :wa_priority,
                :last_update_time, (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            )
            ON CONFLICT (phone10) DO UPDATE SET
                phone = EXCLUDED.phone,
                normalized_phone = EXCLUDED.normalized_phone,
                tag_to = EXCLUDED.tag_to,
                city = EXCLUDED.city,
                team = EXCLUDED.team,
                login_status = EXCLUDED.login_status,
                username = EXCLUDED.username,
                auto_lead = EXCLUDED.auto_lead,
                assign_lead = EXCLUDED.assign_lead,
                wa_priority = EXCLUDED.wa_priority,
                last_update_time = EXCLUDED.last_update_time,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            """
        )

        self._bulk_upsert_in_chunks(sql, payload, 1000)

        new_last_ts = max(
            (r["last_update_time"] for r in payload if r.get("last_update_time") is not None),
            default=cp.get("last_timestamp"),
        )

        self.checkpoint.update_success(
            table,
            last_id=0,
            last_timestamp=new_last_ts,
            batch_count=len(payload),
            notes=f"daily MobileTagging refresh; target=staging_staff_phone_assignment; source_rows={len(rows)}",
        )

        return {
            "inserted_or_updated": len(payload),
            "staff_phone_assignments_refreshed": len(payload),
            "last_id": 0,
            "last_timestamp": new_last_ts,
        }

    def sync_admin_user_accounts(self, limit: int = 50000, mode: str = "daily"):
        """
        Reconcile the complete current RMS caretaker set with PostgreSQL.

        - Every current MySQL caretaker is upserted and marked Active.
        - Every historical PostgreSQL caretaker absent from the current MySQL
          caretaker set is marked Inactive. This includes deleted source accounts
          and accounts whose source team changed to 0/NULL/another team.
        - The PostgreSQL team remains Caretaker for inactive historical records.
        """
        table = "admin_user_account"
        checkpoint_version = "caretaker_full_reconciliation_v3"
        normalized_mode = (mode or "daily").strip().lower()
        force_refresh = normalized_mode in {"force", "full", "refresh", "manual"}

        cp = self.checkpoint.get_checkpoint(table)

        updated_at = cp.get("updated_at")
        checkpoint_notes = str(cp.get("notes") or "")
        current_date = self.db.execute(text("SELECT CURRENT_DATE")).scalar()
        already_refreshed_today = False
        if updated_at is not None and cp.get("last_status") == "SUCCESS":
            try:
                already_refreshed_today = updated_at.date() >= current_date
            except Exception:
                already_refreshed_today = str(updated_at)[:10] >= str(current_date)

        checkpoint_matches_code = checkpoint_version in checkpoint_notes
        if already_refreshed_today and checkpoint_matches_code and not force_refresh:
            return {
                "inserted_or_updated": 0,
                "skipped": True,
                "reason": "admin_user_account already refreshed today; use mode=force to refresh again",
                "last_id": cp.get("last_id"),
                "last_timestamp": cp.get("last_timestamp"),
            }

        existing_caretaker_rows = self.db.execute(
            text(
                f"""
                SELECT source_id, username
                FROM "{SCHEMA_NAME}".staging_user_account
                WHERE LOWER(TRIM(COALESCE(team::text, ''))) = 'caretaker'
                  AND source_id IS NOT NULL
                ORDER BY source_id
                """
            )
        ).mappings().all()

        existing_caretakers = {}
        for row in existing_caretaker_rows:
            source_id = self.safe_int(row.get("source_id"))
            if source_id is None:
                continue
            existing_caretakers[source_id] = self._clean_text(row.get("username"))

        source_engine = get_thirdparty_mysql_engine()
        source_rows = fetch_all(
            source_engine,
            """
            SELECT
                a.id,
                a.username,
                a.email,
                a.contact_no,
                a.is_admin,
                a.team,
                a.createdon,
                a.last_login_time,
                d.contact_num
            FROM a3m_account a
            LEFT JOIN a3m_account_details d ON d.account_id = a.id
            WHERE LOWER(TRIM(CAST(a.team AS CHAR))) = 'caretaker'
            ORDER BY a.id
            """,
            {},
        )

        current_caretakers = {}
        for row in source_rows:
            source_row = dict(row)
            source_id = self.safe_int(source_row.get("id"))
            if source_id is None:
                continue
            current_caretakers[source_id] = source_row

        if existing_caretakers and not current_caretakers:
            raise RuntimeError(
                "MySQL returned zero current caretakers; refusing to mark every "
                "historical PostgreSQL caretaker inactive."
            )

        active_payload = []
        for source_id, source_row in current_caretakers.items():
            phone_raw = source_row.get("contact_num") or source_row.get("contact_no")
            active_payload.append(
                {
                    "source_id": source_id,
                    "username": self._clean_text(source_row.get("username")),
                    "email": self._clean_lower_email(source_row.get("email")),
                    "phone_number": self._clean_text(phone_raw),
                    "normalized_phone": self.norm_phone(phone_raw),
                    "is_admin": self._clean_text(source_row.get("is_admin")),
                    "team": "Caretaker",
                    "active": "Active",
                    "created_on": self.safe_dt(source_row.get("createdon")),
                    "last_login_time": self.safe_dt(source_row.get("last_login_time")),
                }
            )

        inactive_source_ids = sorted(
            set(existing_caretakers) - set(current_caretakers)
        )
        inactive_payload = [
            {"source_id": source_id, "active": "Inactive"}
            for source_id in inactive_source_ids
        ]

        active_upsert_sql = text(
            f"""
            INSERT INTO "{SCHEMA_NAME}".staging_user_account (
                source_id, username, email, phone_number, normalized_phone,
                is_admin, team, active, created_on, last_login_time, synced_at
            ) VALUES (
                :source_id, :username, :email, :phone_number, :normalized_phone,
                :is_admin, :team, :active, :created_on, :last_login_time,
                (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            )
            ON CONFLICT (source_id) DO UPDATE SET
                username = EXCLUDED.username,
                email = EXCLUDED.email,
                phone_number = EXCLUDED.phone_number,
                normalized_phone = EXCLUDED.normalized_phone,
                is_admin = EXCLUDED.is_admin,
                team = EXCLUDED.team,
                active = EXCLUDED.active,
                created_on = EXCLUDED.created_on,
                last_login_time = EXCLUDED.last_login_time,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            """
        )
        inactive_update_sql = text(
            f"""
            UPDATE "{SCHEMA_NAME}".staging_user_account
            SET
                active = :active,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            WHERE source_id = :source_id
              AND LOWER(TRIM(COALESCE(team::text, ''))) = 'caretaker'
            """
        )

        try:
            if active_payload:
                self.db.execute(active_upsert_sql, active_payload)
            if inactive_payload:
                self.db.execute(inactive_update_sql, inactive_payload)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        newly_added_source_ids = sorted(
            set(current_caretakers) - set(existing_caretakers)
        )
        newly_added_usernames = [
            self._clean_text(current_caretakers[source_id].get("username"))
            for source_id in newly_added_source_ids
        ]
        inactive_usernames = [
            existing_caretakers[source_id]
            for source_id in inactive_source_ids
            if existing_caretakers[source_id] is not None
        ]
        active_count = len(active_payload)
        inactive_count = len(inactive_payload)
        updated_count = active_count + inactive_count

        self.checkpoint.update_success(
            table,
            last_id=cp.get("last_id") or 0,
            last_timestamp=cp.get("last_timestamp"),
            batch_count=updated_count,
            notes=(
                f"{checkpoint_version}; current_source={active_count}; "
                f"new_or_restored={len(newly_added_source_ids)}; "
                f"marked_inactive={inactive_count}; "
                f"mode={normalized_mode}"
            ),
        )

        return {
            "inserted_or_updated": updated_count,
            "admin_refreshed": updated_count,
            "updated": updated_count,
            "caretakers_checked": len(existing_caretakers),
            "active": active_count,
            "inactive": inactive_count,
            "active_by_team": active_count,
            "inactive_by_team": inactive_count,
            "new_or_restored": len(newly_added_source_ids),
            "new_or_restored_source_ids": newly_added_source_ids,
            "new_or_restored_usernames": newly_added_usernames,
            "missing_in_source": inactive_count,
            "missing_source_ids": inactive_source_ids,
            "missing_usernames": inactive_usernames,
            "last_id": cp.get("last_id") or 0,
            "last_timestamp": cp.get("last_timestamp"),
        }
