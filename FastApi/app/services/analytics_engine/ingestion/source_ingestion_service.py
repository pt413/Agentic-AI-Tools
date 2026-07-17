from __future__ import annotations

from sqlalchemy import text

from ..core.config import (
    STAGING_BOOKING_AUDIT,
    STAGING_BOOKING_INVOICE,
    STAGING_BOOKINGS,
    STAGING_USER_CONTACT_INFO,
    STAGING_BUILDINGS,
    STAGING_CALL_LOG,
    STAGING_CHECKIN,
    STAGING_CHECKOUT,
    STAGING_EMAILS,
    STAGING_LEADS,
    STAGING_SITE_VISITS,
    STAGING_TICKETS,
    STAGING_TRAVEL_CART,
    STAGING_USERS,
    STAGING_WEB_VISITS,
    STAGING_WHATSAPP,
    STAGING_WISHLIST,
)
from ..core.utils import (
    extract_emails,
    normalize_email,
    normalize_phone,
    build_whatsapp_conversation_key,
)


class SourceIngestionService:
    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------------
    # Existing processors
    # ------------------------------------------------------------------
    def fetch_call_logs(self, last_source_id=0, batch_size=5000, last_timestamp=None):
        """Fetch rows from staging_call_log_unified for CallLogSync.

        Compatibility:
        - Older CallLogSync calls this with only last_source_id.
        - Newer CallLogSync calls this with last_timestamp so transcript/audio
          enrichments on existing unified rows can be reprocessed.
        """
        common_select = f"""
            SELECT
                source_id,
                executive_id,
                executive_name,
                call_time,
                talk_time_sec,
                call_direction,
                call_result,
                counterparty_phone,
                sales_phone,
                lead_id,
                department,
                audio_url,
                transcript_text,
                transcript_text_eleven_labs,
                translated_text,
                raw_transcripts,
                action_layer,
                context,
                language,
                priority,
                source_call_id,
                filename,
                uploaded_at,
                source_status,
                sync_status,
                synced_at,
                updated_at
            FROM {STAGING_CALL_LOG}
        """

        if last_timestamp is not None:
            sql = common_select + """
            WHERE call_time IS NOT NULL
              AND (
                    COALESCE(updated_at, synced_at) > :last_timestamp
                    OR (
                        COALESCE(updated_at, synced_at) = :last_timestamp
                        AND source_id > :last_source_id
                    )
                  )
            ORDER BY COALESCE(updated_at, synced_at), source_id
            LIMIT :batch_size
            """
            return self.db.execute(
                text(sql),
                {
                    "last_source_id": int(last_source_id or 0),
                    "last_timestamp": last_timestamp,
                    "batch_size": int(batch_size),
                },
            ).fetchall()

        sql = common_select + """
        WHERE source_id > :last_source_id
          AND call_time IS NOT NULL
        ORDER BY source_id
        LIMIT :batch_size
        """

        return self.db.execute(
            text(sql),
            {
                "last_source_id": int(last_source_id or 0),
                "batch_size": int(batch_size),
            },
        ).fetchall()


    def fetch_user_accounts(self, last_source_id=0, batch_size=5000):
        sql = f"""
        SELECT
            source_id,
            username,
            email,
            phone_number,
            normalized_phone,
            is_admin,
            team,
            active,
            created_on,
            last_login_time,
            synced_at
        FROM {STAGING_USERS}
        WHERE source_id > :last_source_id
        ORDER BY source_id
        LIMIT :batch_size
        """

        return self.db.execute(
            text(sql),
            {
                "last_source_id": int(last_source_id),
                "batch_size": int(batch_size),
            },
        ).fetchall()
    def fetch_booking_confirms(self, last_source_id=0, batch_size=5000):
        sql = f"""
        SELECT *
        FROM {STAGING_BOOKINGS}
        WHERE source_id > :last_source_id
        ORDER BY source_id
        LIMIT :batch_size
        """

        return self.db.execute(
            text(sql),
            {
                "last_source_id": int(last_source_id),
                "batch_size": int(batch_size),
            },
        ).fetchall()

    def fetch_leads(self, last_source_id=0, batch_size=5000):
        sql = f"""
        SELECT
            source_id,
            user_id,
            booking_id,
            executive_id,
            created_at,
            closed_at,
            raw_status,
            contact_number,
            contact_number_alt,
            email,
            person_id,
            actor_id,
            is_resolved,
            match_type,
            resolved_at,
            synced_at,
            priority,
            added_by,
            assigned_to,
            generated_by,
            origin
        FROM {STAGING_LEADS}
        WHERE source_id > :last_source_id
        ORDER BY source_id
        LIMIT :batch_size
        """

        return self.db.execute(
            text(sql),
            {
                "last_source_id": int(last_source_id),
                "batch_size": int(batch_size),
            },
        ).fetchall()


    def fetch_user_contact_info(self, last_source_id=0, batch_size=5000):
        sql = f"""
        SELECT
            source_id,
            user_id,
            booking_id,
            email,
            contact_name,
            mobile,
            normalized_mobile,
            added_by,
            added_on,
            synced_at
        FROM {STAGING_USER_CONTACT_INFO}
        WHERE source_id > :last_source_id
        ORDER BY source_id
        LIMIT :batch_size
        """

        return self.db.execute(
            text(sql),
            {
                "last_source_id": int(last_source_id),
                "batch_size": int(batch_size),
            },
        ).fetchall()

    def extract_user_contact_info_event_time(self, row):
        return (
            getattr(row, "added_on", None)
            or getattr(row, "synced_at", None)
        )

    def extract_user_contact_info_contexts(self, row):
        contexts = []
        for context_type, value in (
            ("booking", getattr(row, "booking_id", None)),
            ("user", getattr(row, "user_id", None)),
        ):
            if value not in (None, ""):
                contexts.append((context_type, str(value)))
        return contexts

    def build_user_contact_info_event_meta(self, row):
        meta = {
            "source_id": getattr(row, "source_id", None),
            "booking_id": getattr(row, "booking_id", None),
            "user_id": getattr(row, "user_id", None),
            "email": getattr(row, "email", None),
            "contact_name": getattr(row, "contact_name", None),
            "mobile": getattr(row, "mobile", None),
            "normalized_mobile": getattr(row, "normalized_mobile", None),
            "added_by": getattr(row, "added_by", None),
            "added_on": getattr(row, "added_on", None),
        }
        return {k: v for k, v in meta.items() if v not in (None, "")}

    # ------------------------------------------------------------------
    # New processors
    # ------------------------------------------------------------------
    def fetch_site_visits(self, last_source_id=0, batch_size=5000):
        sql = f"""
        SELECT *
        FROM {STAGING_SITE_VISITS}
        WHERE source_id > :last_source_id
        ORDER BY source_id
        LIMIT :batch_size
        """
        return self.db.execute(
            text(sql),
            {"last_source_id": int(last_source_id), "batch_size": int(batch_size)},
        ).fetchall()

    def fetch_travel_cart(self, last_source_id=0, batch_size=5000):
        sql = f"""
        SELECT *
        FROM {STAGING_TRAVEL_CART}
        WHERE source_id > :last_source_id
        ORDER BY source_id
        LIMIT :batch_size
        """
        return self.db.execute(
            text(sql),
            {"last_source_id": int(last_source_id), "batch_size": int(batch_size)},
        ).fetchall()

    def fetch_wishlist_since(self, last_timestamp=None, last_source_id=None, batch_size=5000):
        sql = f"""
        SELECT *
        FROM {STAGING_WISHLIST}
        WHERE added_on IS NOT NULL
          AND (
                :last_timestamp IS NULL
                OR added_on > :last_timestamp
                OR (added_on = :last_timestamp AND source_id > :last_source_id)
              )
        ORDER BY added_on, source_id
        LIMIT :batch_size
        """
        return self.db.execute(
            text(sql),
            {
                "last_timestamp": last_timestamp,
                "last_source_id": int(last_source_id or 0),
                "batch_size": int(batch_size),
            },
        ).fetchall()

    def fetch_whatsapp_messages_since(self, last_timestamp=None, last_source_id=None, batch_size=5000):
        sql = f"""
        SELECT *
        FROM {STAGING_WHATSAPP}
        WHERE message_time IS NOT NULL
          AND (
                :last_timestamp IS NULL
                OR message_time > :last_timestamp
                OR (message_time = :last_timestamp AND source_id > :last_source_id)
              )
        ORDER BY message_time, source_id
        LIMIT :batch_size
        """
        return self.db.execute(
            text(sql),
            {
                "last_timestamp": last_timestamp,
                "last_source_id": str(last_source_id or ""),
                "batch_size": int(batch_size),
            },
        ).fetchall()

    def fetch_web_visits(self, last_source_id=0, batch_size=5000):
        sql = f"""
        SELECT *
        FROM {STAGING_WEB_VISITS}
        WHERE source_id > :last_source_id
        ORDER BY source_id
        LIMIT :batch_size
        """
        return self.db.execute(
            text(sql),
            {"last_source_id": int(last_source_id), "batch_size": int(batch_size)},
        ).fetchall()

    def fetch_checkin_forms(self, last_source_id=0, batch_size=5000):
        sql = f"""
        SELECT *
        FROM {STAGING_CHECKIN}
        WHERE source_id > :last_source_id
        ORDER BY source_id
        LIMIT :batch_size
        """
        return self.db.execute(
            text(sql),
            {"last_source_id": int(last_source_id), "batch_size": int(batch_size)},
        ).fetchall()

    def fetch_checkout_forms(self, last_source_id=0, batch_size=5000):
        sql = f"""
        SELECT
            c.*,
            b.lead_id AS booking_lead_id,
            b.prop_id AS booking_prop_id,
            b.user_id AS booking_user_id
        FROM {STAGING_CHECKOUT} c
        LEFT JOIN {STAGING_BOOKINGS} b
          ON b.source_id = c.booking_id
        WHERE c.source_id > :last_source_id
        ORDER BY c.source_id
        LIMIT :batch_size
        """
        return self.db.execute(
            text(sql),
            {"last_source_id": int(last_source_id), "batch_size": int(batch_size)},
        ).fetchall()

    def fetch_user_tickets(self, last_source_id=None, last_timestamp=None, batch_size=5000):
        sql = f"""
        SELECT
            source_id,
            booking_id,
            prop_id,
            building_id,
            building_name,
            category,
            priority,
            description,
            mobile_number,
            unit_number,
            status,
            reopen_flag,
            created_at,
            assigned_to,
            building_supervisor,
            finance_supervisor,
            building_caretaker,
            coordinator,
            team,
            resolved_by,
            closed_by,
            close_date,
            labour_cost,
            material_cost,
            total_cost,
            active_days,
            ticket_rating,
            ticket_feedback,
            synced_at
        FROM {STAGING_TICKETS}
        WHERE created_at IS NOT NULL
        AND (
                :last_timestamp IS NULL
                OR created_at > :last_timestamp
                OR (created_at = :last_timestamp AND source_id > :last_source_id)
            )
        ORDER BY created_at, source_id
        LIMIT :batch_size
        """
        return self.db.execute(
            text(sql),
            {
                "last_source_id": int(last_source_id or 0),
                "last_timestamp": last_timestamp,
                "batch_size": int(batch_size),
            },
        ).fetchall()

    def fetch_email_messages(self, last_source_id=0, batch_size=5000):
        sql = f"""
        SELECT *
        FROM {STAGING_EMAILS}
        WHERE source_id > :last_source_id
        ORDER BY source_id
        LIMIT :batch_size
        """
        return self.db.execute(
            text(sql),
            {"last_source_id": int(last_source_id), "batch_size": int(batch_size)},
        ).fetchall()

    def fetch_booking_audit_history(self, last_source_id=0, batch_size=5000):
        sql = f"""
        SELECT
            a.*,
            b.lead_id AS booking_lead_id,
            b.prop_id AS booking_prop_id,
            b.user_id AS booking_user_id,
            b.booking_status AS booking_status
        FROM {STAGING_BOOKING_AUDIT} a
        LEFT JOIN {STAGING_BOOKINGS} b
          ON b.source_id = a.booking_id
        WHERE a.source_id > :last_source_id
        ORDER BY a.source_id
        LIMIT :batch_size
        """
        return self.db.execute(
            text(sql),
            {"last_source_id": int(last_source_id), "batch_size": int(batch_size)},
        ).fetchall()

    def fetch_booking_invoice_details(self, last_source_id=None, last_timestamp=None, batch_size=5000):
        sql = f"""
        SELECT
            i.source_id,
            i.booking_id,
            i.payment_id,
            i.amount_status,
            i.duration_period,
            i.mail_status,
            i.sa_mail_status,
            i.reminder_mail,
            i.amount_recieved,
            i.amount,
            i.total_amount,
            i.disc,
            i.from_date,
            i.till_date,
            i.pending_balance,
            i.payment_mode,
            i.comment,
            i.status,
            i.mail_count,
            i.send_time,
            i.modify_flag,
            i.transaction_type,
            i.created_on,
            i.utr_no,
            i.utr_added_by,
            i.om_rent,
            i.sa_rent,
            i.rent_receipt_dw,
            i.sa_receipt_dw,
            i.utr_added_on,
            i.synced_at,
            b.lead_id AS booking_lead_id,
            b.prop_id AS booking_prop_id,
            b.user_id AS booking_user_id,
            b.booking_status AS booking_status
        FROM {STAGING_BOOKING_INVOICE} i
        LEFT JOIN {STAGING_BOOKINGS} b
        ON b.source_id = i.booking_id
        WHERE COALESCE(i.utr_added_on, i.send_time, i.created_on, i.synced_at) IS NOT NULL
        AND (
                :last_timestamp IS NULL
                OR COALESCE(i.utr_added_on, i.send_time, i.created_on, i.synced_at) > :last_timestamp
                OR (
                    COALESCE(i.utr_added_on, i.send_time, i.created_on, i.synced_at) = :last_timestamp
                    AND i.source_id > :last_source_id
                )
            )
        ORDER BY COALESCE(i.utr_added_on, i.send_time, i.created_on, i.synced_at), i.source_id
        LIMIT :batch_size
        """
        return self.db.execute(
            text(sql),
            {
                "last_source_id": int(last_source_id or 0),
                "last_timestamp": last_timestamp,
                "batch_size": int(batch_size),
            },
        ).fetchall()
    def normalize_actor_ref(self, value):
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def is_system_actor(self, value):
        text = self.normalize_actor_ref(value)
        if not text:
            return False

        normalized = text.lower()
        return normalized in {
            "system",
            "auto",
            "automation",
            "automated",
            "cron",
            "scheduler",
            "script",
            "bulk_upload",
            "import",
            "migration",
            "bot",
            "api",
            "backend",
            "server",
        }

    def clean_actor_ref(self, value):
        text = self.normalize_actor_ref(value)
        if not text:
            return None
        if self.is_system_actor(text):
            return None
        return text
    
    def fetch_building_details(self, last_source_id=None, last_timestamp=None, batch_size=5000):
        """
        Fetch building master rows from staging using a time cursor.

        This is intentionally time-cursor based rather than plain id-cursor based
        because building rows are mutable: the same building_id/source_id can be
        updated in staging after it was already processed once. Using synced_at
        ensures those updates are propagated into building_fact.
        """
        sql = f"""
        SELECT
            source_id,
            building_id,
            building_name,
            area,
            address,
            pincode,
            glat,
            glng,
            caretaker,
            supervisor,
            ops_manager,
            finance_supervisor,
            sales,
            marketing,
            rent_model,
            synced_at
        FROM {STAGING_BUILDINGS}
        WHERE synced_at IS NOT NULL
          AND (
                :last_timestamp IS NULL
                OR synced_at > :last_timestamp
                OR (synced_at = :last_timestamp AND source_id > :last_source_id)
              )
        ORDER BY synced_at, source_id
        LIMIT :batch_size
        """
        return self.db.execute(
            text(sql),
            {
                "last_source_id": int(last_source_id or 0),
                "last_timestamp": last_timestamp,
                "batch_size": int(batch_size),
            },
        ).fetchall() 
    
    # ------------------------------------------------------------------
    # Existing participant/context helpers
    # ------------------------------------------------------------------
    def extract_call_participants(self, row):
        counterparty_phone = normalize_phone(getattr(row, "counterparty_phone", None))
        sales_phone = normalize_phone(getattr(row, "sales_phone", None))
        return counterparty_phone, sales_phone

    def extract_booking_participants(self, row):
        customer_phone = normalize_phone(
            getattr(row, "phone_number", None)
            or getattr(row, "mobile_number", None)
            or getattr(row, "customer_phone", None)
            or getattr(row, "guest_phone", None)
        )

        sales_phone = normalize_phone(
            getattr(row, "sales_phone", None)
            or getattr(row, "agent_phone", None)
            or getattr(row, "rm_phone", None)
        )

        created_by = self.clean_actor_ref(getattr(row, "created_by", None))

        executive_id = created_by or self.clean_actor_ref(
            getattr(row, "executive_id", None)
            or getattr(row, "sales_executive_id", None)
            or getattr(row, "agent_id", None)
            or getattr(row, "assigned_to", None)
        )

        return customer_phone, sales_phone, executive_id    
    def extract_lead_participants(self, row):
        primary_phone = normalize_phone(getattr(row, "contact_number", None))
        alt_phone = normalize_phone(getattr(row, "contact_number_alt", None))
        email = normalize_email(getattr(row, "email", None))

        executive_ref = self.clean_actor_ref(
            getattr(row, "executive_id", None)
            or getattr(row, "assigned_to", None)
        )

        return primary_phone, alt_phone, email, executive_ref

    def extract_booking_event_time(self, row):
        return (
            getattr(row, "booking_datetime", None)
            or getattr(row, "booking_confirm_time", None)
            or getattr(row, "booking_time", None)
            or getattr(row, "confirmed_at", None)
            or getattr(row, "created_at", None)
            or getattr(row, "inserted_at", None)
            or getattr(row, "synced_at", None)
        )

    def extract_lead_event_time(self, row):
        return (
            getattr(row, "created_at", None)
            or getattr(row, "resolved_at", None)
            or getattr(row, "synced_at", None)
        )

    def extract_booking_status(self, row):
        return (
            getattr(row, "booking_status", None)
            or getattr(row, "host_confirm_status", None)
            or getattr(row, "status", None)
            or getattr(row, "confirmation_status", None)
            or "confirmed"
        )

    def extract_booking_metric_value(self, row):
        return (
            getattr(row, "total_amount", None)
            or getattr(row, "advance_amount", None)
            or getattr(row, "booking_amount", None)
            or getattr(row, "amount", None)
            or getattr(row, "final_amount", None)
            or getattr(row, "net_amount", None)
            or getattr(row, "gross_amount", None)
        )

    def extract_booking_metric_name(self, row):
        value = self.extract_booking_metric_value(row)
        return "booking_value" if value is not None else None

    def extract_booking_contexts(self, row):
        contexts = []

        lead_id = getattr(row, "lead_id", None)
        if lead_id:
            contexts.append(("lead", str(lead_id)))

        booking_id = self.extract_booking_id(row)
        if booking_id:
            contexts.append(("booking", str(booking_id)))

        property_id = self.extract_booking_property_id(row)
        if property_id:
            contexts.append(("property", str(property_id)))

        user_id = getattr(row, "user_id", None)
        if user_id:
            contexts.append(("user", str(user_id)))

        return contexts

    def extract_lead_contexts(self, row):
        contexts = []

        lead_id = getattr(row, "source_id", None)
        if lead_id:
            contexts.append(("lead", str(lead_id)))

        booking_id = getattr(row, "booking_id", None)
        if booking_id:
            contexts.append(("booking", str(booking_id)))

        user_id = getattr(row, "user_id", None)
        if user_id:
            contexts.append(("user", str(user_id)))

        person_id = getattr(row, "person_id", None)
        if person_id:
            contexts.append(("person", str(person_id)))

        actor_id = getattr(row, "actor_id", None)
        if actor_id:
            contexts.append(("actor", str(actor_id)))

        return contexts

    # ------------------------------------------------------------------
    # New participant/context helpers
    # ------------------------------------------------------------------
    def extract_site_visit_event_time(self, row):
        return (
            getattr(row, "site_visit_date", None)
            or getattr(row, "added_on", None)
            or getattr(row, "synced_at", None)
        )

    def extract_site_visit_contexts(self, row):
        contexts = []
        for context_type, value in (
            ("lead", getattr(row, "lead_id", None)),
            ("property", getattr(row, "prop_id", None)),
            ("building", getattr(row, "building_id", None)),
        ):
            if value not in (None, ""):
                contexts.append((context_type, str(value)))
        return contexts

    def extract_travel_cart_event_time(self, row):
        return getattr(row, "added_on", None) or getattr(row, "synced_at", None)

    def extract_travel_cart_contexts(self, row):
        contexts = []
        for context_type, value in (
            ("user", getattr(row, "user_id", None)),
            ("property", getattr(row, "prop_id", None)),
        ):
            if value not in (None, ""):
                contexts.append((context_type, str(value)))
        return contexts

    def extract_wishlist_event_time(self, row):
        return getattr(row, "added_on", None) or getattr(row, "synced_at", None)

    def extract_wishlist_contexts(self, row):
        return self.extract_travel_cart_contexts(row)

    def extract_whatsapp_participants(self, row):
        customer_phone = normalize_phone(getattr(row, "cx_number", None))
        admin_phone = normalize_phone(getattr(row, "admin_number", None))
        executive_ref = self.clean_actor_ref(getattr(row, "executive_id", None))
        return customer_phone, admin_phone, executive_ref

    def build_whatsapp_thread_key(
        self,
        row=None,
        *,
        customer_phone=None,
        admin_phone=None,
        remote_jid=None,
    ):
        """
        Stable WhatsApp conversation/thread key.

        Priority:
        1. remote_jid, because group chats and LID/direct JIDs should remain stable.
        2. admin/customer phone pair.
        3. customer phone.
        4. admin phone.
        """
        if row is not None:
            remote_jid = remote_jid or getattr(row, "remote_jid", None)
            customer_phone = customer_phone or getattr(row, "cx_number", None)
            admin_phone = admin_phone or getattr(row, "admin_number", None)

        return build_whatsapp_conversation_key(
            customer_phone=customer_phone,
            admin_phone=admin_phone,
            remote_jid=remote_jid,
        )

    def infer_whatsapp_conversation_kind(
        self,
        row=None,
        *,
        remote_jid=None,
        customer_phone=None,
        admin_phone=None,
    ):
        """
        Infer whether WhatsApp message belongs to group or direct conversation.

        Baileys LID direct chats can have remote_jid ending with @lid,
        so only @g.us should be treated as group.
        """
        if row is not None:
            remote_jid = remote_jid or getattr(row, "remote_jid", None)
            customer_phone = customer_phone or getattr(row, "cx_number", None)
            admin_phone = admin_phone or getattr(row, "admin_number", None)

        remote_text = str(remote_jid or "").strip().lower()

        if remote_text.endswith("@g.us"):
            return "group"

        if remote_text or customer_phone or admin_phone:
            return "direct"

        return "unknown"

    def extract_whatsapp_contexts(self, row):
        contexts = []
        for context_type, value in (
            ("lead", getattr(row, "lead_id", None)),
            ("remote_jid", getattr(row, "remote_jid", None)),
        ):
            if value not in (None, ""):
                contexts.append((context_type, str(value)))
        return contexts

    def extract_web_visit_event_time(self, row):
        return (
            getattr(row, "event_time", None)
            or getattr(row, "visited_at", None)
            or getattr(row, "created_at", None)
            or getattr(row, "synced_at", None)
        )

    def extract_web_visit_contexts(self, row):
        contexts = []
        for context_type, value in (
            ("user", getattr(row, "user_id", None)),
            ("lead", getattr(row, "lead_id", None)),
            ("property", getattr(row, "prop_id", None)),
            ("session", getattr(row, "session_id", None)),
        ):
            if value not in (None, ""):
                contexts.append((context_type, str(value)))
        return contexts

    def extract_checkin_event_time(self, row):
        return getattr(row, "checkin_date", None) or getattr(row, "added_on", None) or getattr(row, "synced_at", None)

    def extract_checkout_event_time(self, row):
        return getattr(row, "checkout_date", None) or getattr(row, "added_time", None) or getattr(row, "synced_at", None)

    def extract_checkin_participants(self, row):
        return {
            "user_email": normalize_email(getattr(row, "user_email", None)),
            "supervisor": getattr(row, "supervisor", None),
            "caretaker": getattr(row, "caretaker", None),
            "ops_manager": getattr(row, "ops_manager", None),
            "salesperson": getattr(row, "salesperson", None),
        }

    def extract_checkout_participants(self, row):
        return self.extract_checkin_participants(row)

    def extract_checkin_contexts(self, row):
        contexts = []
        for context_type, value in (
            ("booking", getattr(row, "booking_id", None)),
            ("property", getattr(row, "prop_id", None)),
        ):
            if value not in (None, ""):
                contexts.append((context_type, str(value)))
        return contexts

    def extract_checkout_contexts(self, row):
        contexts = []
        for context_type, value in (
            ("booking", getattr(row, "booking_id", None)),
            ("property", getattr(row, "booking_prop_id", None)),
            ("lead", getattr(row, "booking_lead_id", None)),
            ("user", getattr(row, "booking_user_id", None)),
        ):
            if value not in (None, ""):
                contexts.append((context_type, str(value)))
        return contexts

    def extract_ticket_created_time(self, row):
        return getattr(row, "created_at", None) or getattr(row, "synced_at", None)

    def extract_ticket_resolved_time(self, row):
        return getattr(row, "close_date", None)

    def extract_user_ticket_participants(self, row):
        return {
            "mobile_number": normalize_phone(getattr(row, "mobile_number", None)),
            "assigned_to": self.clean_actor_ref(getattr(row, "assigned_to", None)),
            "building_supervisor": self.clean_actor_ref(getattr(row, "building_supervisor", None)),
            "finance_supervisor": self.clean_actor_ref(getattr(row, "finance_supervisor", None)),
            "building_caretaker": self.clean_actor_ref(getattr(row, "building_caretaker", None)),
            "coordinator": self.clean_actor_ref(getattr(row, "coordinator", None)),
            "resolved_by": self.clean_actor_ref(getattr(row, "resolved_by", None)),
            "closed_by": self.clean_actor_ref(getattr(row, "closed_by", None)),
        }

    def extract_user_ticket_contexts(self, row):
        contexts = []
        for context_type, value in (
            ("booking", getattr(row, "booking_id", None)),
            ("property", getattr(row, "prop_id", None)),
            ("building", getattr(row, "building_id", None)),
        ):
            if value not in (None, ""):
                contexts.append((context_type, str(value)))
        return contexts

    def extract_email_event_time(self, row):
        return getattr(row, "email_date", None) or getattr(row, "synced_at", None)

    def extract_email_participants(self, row):
        return {
            "sender_emails": extract_emails(getattr(row, "sender", None)),
            "receiver_emails": extract_emails(getattr(row, "receiver", None)),
        }

    def extract_email_contexts(self, row):
        contexts = []
        thread_id = getattr(row, "thread_id", None)
        if thread_id not in (None, ""):
            contexts.append(("thread", str(thread_id)))
        return contexts

    def extract_booking_audit_event_time(self, row):
        return getattr(row, "added_time", None) or getattr(row, "synced_at", None)

    def extract_booking_audit_contexts(self, row):
        contexts = []
        for context_type, value in (
            ("booking", getattr(row, "booking_id", None)),
            ("lead", getattr(row, "booking_lead_id", None)),
            ("property", getattr(row, "booking_prop_id", None)),
            ("user", getattr(row, "booking_user_id", None)),
        ):
            if value not in (None, ""):
                contexts.append((context_type, str(value)))
        return contexts

    def extract_booking_invoice_cursor_time(self, row):
        return (
            getattr(row, "utr_added_on", None)
            or getattr(row, "send_time", None)
            or getattr(row, "created_on", None)
            or getattr(row, "synced_at", None)
        )
    def build_lead_customer_candidate_keys(self, row):
        keys = []

        user_id = getattr(row, "user_id", None)
        if user_id not in (None, ""):
            keys.append(("user_id", str(user_id).strip()))

        primary_phone = normalize_phone(getattr(row, "contact_number", None))
        if primary_phone:
            keys.append(("phone", primary_phone))

        alt_phone = normalize_phone(getattr(row, "contact_number_alt", None))
        if alt_phone:
            keys.append(("phone", alt_phone))

        email = normalize_email(getattr(row, "email", None))
        if email:
            keys.append(("email", email))

        return self._dedupe_keys(keys)


    def build_lead_customer_seed_fields(self, row):
        return {
            "primary_phone": normalize_phone(getattr(row, "contact_number", None)),
            "primary_email": normalize_email(getattr(row, "email", None)),
            "canonical_name": None,
        }
    def extract_booking_invoice_contexts(self, row):
        contexts = []

        booking_id = getattr(row, "booking_id", None)
        if booking_id not in (None, "", 0, "0"):
            contexts.append(("booking", str(booking_id)))

        lead_id = getattr(row, "booking_lead_id", None)
        if lead_id not in (None, "", 0, "0"):
            contexts.append(("lead", str(lead_id)))

        property_id = getattr(row, "booking_prop_id", None)
        if property_id not in (None, "", 0, "0"):
            contexts.append(("property", str(property_id)))

        user_id = getattr(row, "booking_user_id", None)
        if user_id not in (None, "", 0, "0"):
            contexts.append(("user", str(user_id)))

        payment_id = getattr(row, "payment_id", None)
        if payment_id not in (None, "", 0, "0"):
            contexts.append(("payment", str(payment_id)))

        utr_no = getattr(row, "utr_no", None)
        if utr_no not in (None, ""):
            utr_value = str(utr_no).strip()
            if utr_value:
                contexts.append(("utr", utr_value))

        return contexts

    # ------------------------------------------------------------------
    # Existing booking/lead meta helpers
    # ------------------------------------------------------------------
    def extract_booking_id(self, row):
        return (
            getattr(row, "booking_id", None)
            or getattr(row, "reservation_id", None)
            or getattr(row, "order_id", None)
        )

    def extract_booking_property_id(self, row):
        return (
            getattr(row, "prop_id", None)
            or getattr(row, "property_id", None)
            or getattr(row, "listing_id", None)
            or getattr(row, "unit_id", None)
        )

    def extract_booking_currency_code(self, row):
        return (
            getattr(row, "currency_code", None)
            or getattr(row, "currency", None)
            or getattr(row, "currency_iso", None)
        )

    def build_booking_meta(self, row):
        keys = [
            "source_id",
            "booking_id",
            "user_id",
            "lead_id",
            "prop_id",
            "booking_status",
            "host_confirm_status",
            "refund_status",
            "no_show_status",
            "booking_type",
            "period",
            "type_of_booking",
            "booking_datetime",
            "travel_from_date",
            "travel_to_date",
            "nights",
            "total_amount",
            "early_cout",
            "before_disc_monthly",
            "after_disc_month_rent",
            "num_guests",
            "renv_gst",
            "total_taxes",
            "service_charge",
            "advance_amount",
            "paid_advance_amount",
            "advance_percent",
            "om_rent",
            "sa_rent",
            "rent_margin",
            "check_in_time",
            "check_out_time",
            "no_show_time",
            "txn_source",
            "booking_source",
            "created_by",
            "person_id",
            "actor_id",
            "is_resolved",
            "match_type",
            "resolved_at",
            "synced_at",
        ]

        meta = {}
        for key in keys:
            value = getattr(row, key, None)
            if value is not None:
                meta[key] = value

        return meta

    def build_booking_event_meta(self, row):
        meta = {
            "source_id": getattr(row, "source_id", None),
            "booking_id": self.extract_booking_id(row),
            "lead_id": getattr(row, "lead_id", None),
            "property_id": self.extract_booking_property_id(row),
            "user_id": getattr(row, "user_id", None),
            "booking_status": self.extract_booking_status(row),
            "booking_type": getattr(row, "booking_type", None),
            "booking_source": getattr(row, "booking_source", None),
        }

        return {k: v for k, v in meta.items() if v is not None}

    def build_booking_fact_payload(self, row):
        customer_phone, sales_phone, executive_id = self.extract_booking_participants(row)

        return {
            "booking_id": self.extract_booking_id(row),
            "lead_id": getattr(row, "lead_id", None),
            "property_id": self.extract_booking_property_id(row),
            "customer_phone": customer_phone,
            "sales_phone": sales_phone,
            "executive_ref": str(executive_id).strip() if executive_id is not None else None,
            "booking_status": self.extract_booking_status(row),
            "booking_amount": self.extract_booking_metric_value(row),
            "currency_code": self.extract_booking_currency_code(row),
            "booking_time": self.extract_booking_event_time(row),
            "raw_payload": self.build_booking_meta(row),
        }

    def build_lead_event_meta(self, row):
        meta = {
            "source_id": getattr(row, "source_id", None),
            "user_id": getattr(row, "user_id", None),
            "booking_id": getattr(row, "booking_id", None),
            "executive_id": getattr(row, "executive_id", None),
            "raw_status": getattr(row, "raw_status", None),
            "is_resolved": getattr(row, "is_resolved", None),
            "match_type": getattr(row, "match_type", None),
            "priority": getattr(row, "priority", None),
            "origin": getattr(row, "origin", None),
            "created_at": getattr(row, "created_at", None),
            "closed_at": getattr(row, "closed_at", None),
            "resolved_at": getattr(row, "resolved_at", None),
            "synced_at": getattr(row, "synced_at", None),
        }

        return {k: v for k, v in meta.items() if v is not None}

    def build_lead_fact_payload(self, row):
        primary_phone, alt_phone, email, executive_ref = self.extract_lead_participants(row)

        return {
            "lead_id": getattr(row, "source_id", None),
            "booking_id": getattr(row, "booking_id", None),
            "user_id": getattr(row, "user_id", None),
            "person_id": getattr(row, "person_id", None),
            "actor_id": getattr(row, "actor_id", None),
            "executive_ref": str(executive_ref).strip() if executive_ref is not None else None,
            "assigned_to": getattr(row, "assigned_to", None),
            "added_by": getattr(row, "added_by", None),
            "generated_by": getattr(row, "generated_by", None),
            "origin": getattr(row, "origin", None),
            "raw_status": getattr(row, "raw_status", None),
            "is_resolved": getattr(row, "is_resolved", None),
            "match_type": getattr(row, "match_type", None),
            "resolved_at": getattr(row, "resolved_at", None),
            "created_at_source": getattr(row, "created_at", None),
            "closed_at_source": getattr(row, "closed_at", None),
            "synced_at_source": getattr(row, "synced_at", None),
            "priority": getattr(row, "priority", None),
            "contact_number": primary_phone,
            "contact_number_alt": alt_phone,
            "email": email,
            "raw_payload": {
                "source_id": getattr(row, "source_id", None),
                "user_id": getattr(row, "user_id", None),
                "booking_id": getattr(row, "booking_id", None),
                "executive_id": getattr(row, "executive_id", None),
                "created_at": getattr(row, "created_at", None),
                "closed_at": getattr(row, "closed_at", None),
                "raw_status": getattr(row, "raw_status", None),
                "contact_number": getattr(row, "contact_number", None),
                "contact_number_alt": getattr(row, "contact_number_alt", None),
                "email": getattr(row, "email", None),
                "person_id": getattr(row, "person_id", None),
                "actor_id": getattr(row, "actor_id", None),
                "is_resolved": getattr(row, "is_resolved", None),
                "match_type": getattr(row, "match_type", None),
                "resolved_at": getattr(row, "resolved_at", None),
                "synced_at": getattr(row, "synced_at", None),
                "priority": getattr(row, "priority", None),
                "added_by": getattr(row, "added_by", None),
                "assigned_to": getattr(row, "assigned_to", None),
                "generated_by": getattr(row, "generated_by", None),
                "origin": getattr(row, "origin", None),
            },
        }

    def build_user_account_candidate_keys(self, row):
        keys = []

        username = getattr(row, "username", None)
        if username:
            username = str(username).strip().lower()
            if username:
                keys.append(("username", username))
        
        user_id = getattr(row, "source_id", None) or getattr(row, "user_id", None)
        if user_id:
            keys.append(("user_id", str(user_id)))
            
        staff_ref = getattr(row, "staff_ref", None)
        if staff_ref:
            staff_ref = str(staff_ref).strip().lower()
            if staff_ref:
                keys.append(("staff_ref", staff_ref))

        email = normalize_email(getattr(row, "email", None))
        if email:
            keys.append(("email", email))

        normalized_phone = getattr(row, "normalized_phone", None)
        if normalized_phone:
            normalized_phone = str(normalized_phone).strip()
            if normalized_phone:
                keys.append(("phone", normalized_phone))
        else:
            phone = normalize_phone(getattr(row, "phone_number", None))
            if phone:
                keys.append(("phone", phone))

        return self._dedupe_keys(keys)

    def build_user_account_match_keys(self, row):
        return self.build_user_account_candidate_keys(row)

    def build_user_account_attach_keys(self, row):
        return []

    def _dedupe_keys(self, keys):
        seen = set()
        result = []

        for key_type, key_value in keys:
            item = (str(key_type).strip().lower(), str(key_value).strip())
            if not item[1]:
                continue
            if item in seen:
                continue
            seen.add(item)
            result.append(item)

        return result
    def fetch_user_wishlist(self, last_source_id=None, last_timestamp=None, batch_size=5000):
        sql = """
        SELECT
            source_id,
            user_id,
            prop_id,
            added_on,
            synced_at
        FROM "AnalyticsEngine".staging_user_wishlist
        WHERE added_on IS NOT NULL
        AND (
                :last_timestamp IS NULL
                OR added_on > :last_timestamp
                OR (added_on = :last_timestamp AND CAST(source_id AS TEXT) > CAST(:last_source_id AS TEXT))
            )
        ORDER BY added_on, CAST(source_id AS TEXT)
        LIMIT :batch_size
        """

        return self.db.execute(
            text(sql),
            {
                "last_source_id": str(last_source_id or ""),
                "last_timestamp": last_timestamp,
                "batch_size": int(batch_size),
            },
        ).fetchall()