from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .cleaning import clean_event_for_mode
from .common import (
    StaffRoleResolver,
    add_business_hours_scoring,
    call_transcript_text,
    classify_text,
    clean_text,
    compact_dict,
    extract_emails,
    fallback_email_role,
    flow_from_direction,
    fmt_dt,
    fmt_duration,
    is_business_to_customer_flow,
    message_kind,
    norm_email,
    phone_last10,
    priority_for_event,
    q,
    q1,
    schema_ident,
    show_phone,
    table_columns,
    table_exists,
    text_for_message_type,
    normalize_direction,
)

def fetch_lead_row(db: Session, schema: str, lead_id: int) -> Dict[str, Any]:
    if not table_exists(db, schema, "staging_lead_tracking"):
        return {"source_id": lead_id}
    return q1(db, f"SELECT * FROM {schema_ident(schema)}.staging_lead_tracking WHERE source_id = :lead_id LIMIT 1", {"lead_id": lead_id}) or {"source_id": lead_id}





def fetch_whatsapp_rows(
    db: Session,
    schema: str,
    lead_id: int,
    contacts: Dict[str, Any],
    start_dt: datetime,
    end_dt: datetime,
    limit: int,
    max_text: int,
    role_resolver: Optional[StaffRoleResolver] = None,
) -> List[Dict[str, Any]]:
    if not table_exists(db, schema, "staging_whatsapp_messages"):
        return []

    phones = contacts.get("phones") or []
    phone10s = sorted({phone_last10(phone) for phone in phones if phone_last10(phone)})

    if not phone10s:
        return []

    placeholders = []
    params: Dict[str, Any] = {
        "start_dt": start_dt,
        "end_dt": end_dt,
        "limit_n": int(limit),
    }

    for idx, phone10 in enumerate(phone10s):
        key = f"phone10_{idx}"
        placeholders.append(f":{key}")
        params[key] = phone10

    phone_in_sql = ", ".join(placeholders)

    rows = q(
        db,
        f"""
        SELECT source_id::text,
               lead_id::text,
               message_time AS event_time,
               direction,
               executive_id,
               message_type,
               clean_content,
               isread,
               issent,
               admin_number,
               cx_number,
               remote_jid
        FROM {schema_ident(schema)}.staging_whatsapp_messages
        WHERE RIGHT(REGEXP_REPLACE(COALESCE(cx_number::text, ''), '\\D', '', 'g'), 10) IN ({phone_in_sql})
          AND message_time >= :start_dt
          AND message_time < :end_dt
        ORDER BY message_time ASC NULLS LAST, source_id ASC
        LIMIT :limit_n
        """,
        params,
    )

    out: List[Dict[str, Any]] = []
    for row in rows:
        text_value = text_for_message_type(row.get("message_type"), row.get("clean_content"))
        if not text_value:
            continue

        kind = message_kind(row.get("message_type"), row.get("clean_content"))
        admin = show_phone(row.get("admin_number"))
        customer = show_phone(row.get("cx_number"))
        resolver = role_resolver or StaffRoleResolver(db, schema)
        actor = resolver.resolve(
            username=row.get("executive_id"),
            phone=admin,
            fallback_role="sales",
            fallback_actor=row.get("executive_id") or admin,
        )
        actor_role = actor.get("actor_role") or "sales"
        flow = flow_from_direction(row.get("direction"), actor_role)
        category = classify_text(text_value, flow, kind)
        if category.startswith("auto_") and is_business_to_customer_flow(flow):
            actor = {"actor_role": "system", "actor": "automation"}
            flow = flow_from_direction(row.get("direction"), "system")
        priority = priority_for_event("whatsapp", flow, category, kind)

        event = add_business_hours_scoring({
            "time": row.get("event_time"),
            "channel": "whatsapp",
            "source_id": row.get("source_id"),
            "lead_id": row.get("lead_id"),
            "flow": flow,
            "direction": normalize_direction(row.get("direction")),
            "status": "read" if str(row.get("isread")).lower() in {"1", "true", "t", "yes"} else "sent" if str(row.get("issent")).lower() in {"1", "true", "t", "yes"} else None,
            "actor_role": actor.get("actor_role"),
            "actor": actor.get("actor"),
            "actor_source": actor.get("actor_source"),
            "admin_number": admin,
            "customer_number": customer,
            "kind": kind,
            "category": category,
            "priority": priority,
            "needs_review": priority in {"P1", "P2"},
            "match_by": f"phone={customer}" if customer else None,
            "text": clean_text(text_value, max_text),
        })
        out.append(clean_event_for_mode(event, mode="raw"))

    return out


def fetch_call_rows(
    db: Session,
    schema: str,
    lead_id: int,
    start_dt: datetime,
    end_dt: datetime,
    limit: int,
    max_text: int,
    role_resolver: Optional[StaffRoleResolver] = None,
) -> List[Dict[str, Any]]:
    if not table_exists(db, schema, "staging_call_log_unified"):
        return []
    rows = q(
        db,
        f"""
        SELECT source_id::text, lead_id::text, executive_id, executive_name,
               call_time AS event_time, call_direction, call_result, talk_time_sec,
               counterparty_phone, sales_phone, audio_url,
               translated_text, transcript_text, transcript_text_eleven_labs, raw_transcripts
        FROM {schema_ident(schema)}.staging_call_log_unified
        WHERE lead_id = :lead_id
          AND call_time >= :start_dt AND call_time < :end_dt
        ORDER BY call_time ASC NULLS LAST, source_id ASC
        LIMIT :limit_n
        """,
        {"lead_id": lead_id, "start_dt": start_dt, "end_dt": end_dt, "limit_n": int(limit)},
    )
    out: List[Dict[str, Any]] = []
    resolver = role_resolver or StaffRoleResolver(db, schema)
    for row in rows:
        admin = show_phone(row.get("sales_phone"))
        customer = show_phone(row.get("counterparty_phone"))
        actor = resolver.resolve(
            username=row.get("executive_id") or row.get("executive_name"),
            phone=admin,
            fallback_role="sales",
            fallback_actor=row.get("executive_id") or row.get("executive_name") or admin,
        )
        flow = flow_from_direction(row.get("call_direction"), actor.get("actor_role") or "sales")
        duration = int(row.get("talk_time_sec") or 0)
        transcript = call_transcript_text(row, max_text)
        category = "manual_call"
        kind = "call_connected" if duration > 0 else "call_missed"
        priority = priority_for_event("call", flow, category, kind)
        event = add_business_hours_scoring({
            "time": row.get("event_time"),
            "channel": "call",
            "source_id": row.get("source_id"),
            "lead_id": row.get("lead_id"),
            "flow": flow,
            "direction": normalize_direction(row.get("call_direction")),
            "status": normalize_direction(row.get("call_result")),
            "actor_role": actor.get("actor_role"),
            "actor": actor.get("actor"),
            "actor_name": clean_text(row.get("executive_name"), 80),
            "actor_source": actor.get("actor_source"),
            "admin_number": admin,
            "customer_number": customer,
            "kind": kind,
            "category": category,
            "priority": priority,
            "duration_sec": duration,
            "needs_review": priority in {"P1", "P2"},
            "text": clean_text(f"Call {row.get('call_result') or '-'} duration={fmt_duration(duration)}", max_text),
            "transcript": transcript,
            "audio_url": row.get("audio_url"),
        })
        out.append(clean_event_for_mode(event, mode="raw"))
    return out






















def _dedupe_keep_order(values: Any) -> List[Any]:
    if values in (None, "", [], {}):
        return []
    source_values = values if isinstance(values, (list, tuple, set)) else [values]
    seen: set[str] = set()
    out: List[Any] = []
    for value in source_values:
        if value in (None, "", 0, "0", [], {}):
            continue
        key = str(value).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def normalized_id_list(values: Any) -> List[str]:
    return [str(value).strip() for value in _dedupe_keep_order(values) if str(value).strip()]


def _append_unique_id(values: List[str], value: Any) -> None:
    if value in (None, "", 0, "0"):
        return
    text_value = str(value).strip()
    if text_value and text_value not in values:
        values.append(text_value)


def _add_unique(values: List[str], value: Any) -> None:
    if value in (None, "", 0, "0"):
        return
    text_value = str(value).strip()
    if text_value and text_value not in values:
        values.append(text_value)


def lead_contacts(lead_row: Dict[str, Any]) -> Dict[str, Any]:
    phones: List[str] = []
    for key in ("contact_number", "contact_number_alt", "contact_details", "contact_details2", "phone", "mobile"):
        p = show_phone(lead_row.get(key))
        if p:
            _add_unique(phones, p)

    emails: List[str] = []
    for key in ("email", "email_id", "user_email"):
        e = norm_email(lead_row.get(key))
        if e:
            _add_unique(emails, e)
        for extracted in extract_emails(lead_row.get(key)):
            _add_unique(emails, extracted)

    booking_ids = normalized_id_list(lead_row.get("booking_id"))
    user_ids = normalized_id_list(lead_row.get("user_id"))

    return compact_dict({
        "phones": phones,
        "emails": emails,
        "booking_ids": booking_ids,
        "booking_id": booking_ids[0] if booking_ids else None,  # raw/debug compatibility only
        "user_ids": user_ids,
        "user_id": user_ids[0] if user_ids else None,            # raw/debug compatibility only
        "person_id": lead_row.get("person_id") if lead_row.get("person_id") not in (None, "", 0, "0") else None,
        "executive_id": lead_row.get("executive_id") or lead_row.get("assigned_to"),
    })


def _collect_call_counterparty_phones(db: Session, schema: str, lead_id: int) -> List[str]:
    if not table_exists(db, schema, "staging_call_log_unified"):
        return []
    rows = q(
        db,
        f"""
        SELECT DISTINCT counterparty_phone
        FROM {schema_ident(schema)}.staging_call_log_unified
        WHERE lead_id = :lead_id
          AND counterparty_phone IS NOT NULL
        """,
        {"lead_id": lead_id},
    )
    phones: List[str] = []
    for row in rows:
        p = show_phone(row.get("counterparty_phone"))
        if p:
            _add_unique(phones, p)
    return phones


def enrich_lead_contacts(db: Session, schema: str, lead_id: int, lead_row: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve sparse lead rows into enough customer keys for email/cart/booking lookup.

    Narrow bridge only: lead_id -> call counterparty phone -> user/contact/booking rows.
    This prevents broad unrelated identity expansion while recovering sparse leads.
    """
    contacts = lead_contacts(lead_row)
    phones: List[str] = list(contacts.get("phones") or [])
    emails: List[str] = list(contacts.get("emails") or [])
    user_ids: List[str] = normalized_id_list(contacts.get("user_ids") or contacts.get("user_id"))
    booking_ids: List[str] = normalized_id_list(contacts.get("booking_ids") or contacts.get("booking_id"))

    for phone in _collect_call_counterparty_phones(db, schema, lead_id):
        _add_unique(phones, phone)

    phone10s = sorted({phone_last10(phone) for phone in phones if phone_last10(phone)})

    if table_exists(db, schema, "staging_user_account") and (phone10s or emails):
        conds: List[str] = []
        params: Dict[str, Any] = {}
        if phone10s:
            holders = []
            for idx, phone10 in enumerate(phone10s):
                key = f"p{idx}"
                holders.append(f":{key}")
                params[key] = phone10
            conds.append("RIGHT(REGEXP_REPLACE(COALESCE(normalized_phone::text, phone_number::text, ''), '\\D', '', 'g'), 10) IN (" + ", ".join(holders) + ")")
        if emails:
            holders = []
            for idx, email in enumerate(emails):
                key = f"e{idx}"
                holders.append(f":{key}")
                params[key] = email.lower()
            conds.append("LOWER(TRIM(COALESCE(email::text, ''))) IN (" + ", ".join(holders) + ")")
        rows = q(
            db,
            f"""
            SELECT source_id, email, phone_number, normalized_phone, is_admin, active
            FROM {schema_ident(schema)}.staging_user_account
            WHERE ({' OR '.join(conds)})
            ORDER BY
              CASE WHEN COALESCE(is_admin::text, '0') IN ('1','true','t','yes','admin') THEN 1 ELSE 0 END,
              active DESC NULLS LAST,
              source_id DESC
            LIMIT 20
            """,
            params,
        )
        for row in rows:
            # Avoid turning staff/admin accounts into customer user ids, but still allow their emails/phones to be ignored naturally.
            if str(row.get("is_admin") or "0").strip().lower() in {"1", "true", "t", "yes", "admin"}:
                continue
            _append_unique_id(user_ids, row.get("source_id"))
            email = norm_email(row.get("email"))
            if email:
                _add_unique(emails, email)
            phone = show_phone(row.get("normalized_phone") or row.get("phone_number"))
            if phone:
                _add_unique(phones, phone)

    if table_exists(db, schema, "staging_user_contact_info") and (phone10s or emails or user_ids):
        conds = []
        params = {}
        if phone10s:
            holders = []
            for idx, phone10 in enumerate(phone10s):
                key = f"uci_p{idx}"
                holders.append(f":{key}")
                params[key] = phone10
            conds.append("RIGHT(REGEXP_REPLACE(COALESCE(normalized_mobile::text, mobile::text, ''), '\\D', '', 'g'), 10) IN (" + ", ".join(holders) + ")")
        if emails:
            holders = []
            for idx, email in enumerate(emails):
                key = f"uci_e{idx}"
                holders.append(f":{key}")
                params[key] = email.lower()
            conds.append("LOWER(TRIM(COALESCE(email::text, ''))) IN (" + ", ".join(holders) + ")")
        if user_ids:
            holders = []
            for idx, user_id in enumerate(user_ids):
                key = f"uci_u{idx}"
                holders.append(f":{key}")
                params[key] = str(user_id)
            conds.append("user_id::text IN (" + ", ".join(holders) + ")")
        rows = q(
            db,
            f"""
            SELECT source_id, user_id, booking_id, email, mobile, normalized_mobile
            FROM {schema_ident(schema)}.staging_user_contact_info
            WHERE {' OR '.join(conds)}
            ORDER BY added_on DESC NULLS LAST, source_id DESC
            LIMIT 50
            """,
            params,
        ) if conds else []
        for row in rows:
            _append_unique_id(user_ids, row.get("user_id"))
            _append_unique_id(booking_ids, row.get("booking_id"))
            email = norm_email(row.get("email"))
            if email:
                _add_unique(emails, email)
            phone = show_phone(row.get("normalized_mobile") or row.get("mobile"))
            if phone:
                _add_unique(phones, phone)

    if table_exists(db, schema, "staging_booking_confirm"):
        conds = ["lead_id::text = :lead_id_text"]
        params = {"lead_id_text": str(lead_id)}
        if user_ids:
            holders = []
            for idx, user_id in enumerate(user_ids):
                key = f"bc_u{idx}"
                holders.append(f":{key}")
                params[key] = str(user_id)
            conds.append("user_id::text IN (" + ", ".join(holders) + ")")
        if booking_ids:
            holders = []
            for idx, booking_id in enumerate(booking_ids):
                key = f"bc_b{idx}"
                holders.append(f":{key}")
                params[key] = str(booking_id)
            conds.append("(booking_id::text IN (" + ", ".join(holders) + ") OR source_id::text IN (" + ", ".join(holders) + "))")
        rows = q(
            db,
            f"""
            SELECT source_id, booking_id, user_id, lead_id
            FROM {schema_ident(schema)}.staging_booking_confirm
            WHERE {' OR '.join(f'({c})' for c in conds)}
            ORDER BY COALESCE(booking_datetime, travel_from_date, synced_at) ASC NULLS LAST, source_id ASC
            LIMIT 50
            """,
            params,
        )
        for row in rows:
            _append_unique_id(user_ids, row.get("user_id"))
            _append_unique_id(booking_ids, row.get("booking_id") or row.get("source_id"))

    return compact_dict({
        "phones": phones,
        "emails": emails,
        "booking_ids": booking_ids,
        "booking_id": booking_ids[0] if booking_ids else None,
        "user_ids": user_ids,
        "user_id": user_ids[0] if user_ids else None,
        "person_id": contacts.get("person_id"),
        "executive_id": contacts.get("executive_id"),
    })



def fetch_email_rows(
    db: Session,
    schema: str,
    lead_email: Any,
    start_dt: datetime,
    end_dt: datetime,
    limit: int,
    max_text: int,
    role_resolver: Optional[StaffRoleResolver] = None,
) -> List[Dict[str, Any]]:
    emails = [email for email in (norm_email(v) for v in _dedupe_keep_order(lead_email)) if email]
    if not emails or not table_exists(db, schema, "staging_email_messages"):
        return []

    holders = []
    params: Dict[str, Any] = {"start_dt": start_dt, "end_dt": end_dt, "limit_n": int(limit)}
    for idx, email in enumerate(emails):
        key = f"email_{idx}"
        holders.append(f":{key}")
        params[key] = email
    email_in_sql = ", ".join(holders)

    rows = q(
        db,
        f"""
        WITH matched AS (
            SELECT source_id, msgid, thread_id, direction, email_date, sender, receiver, subject, snippet, body
            FROM {schema_ident(schema)}.staging_email_messages
            WHERE email_date >= :start_dt AND email_date < :end_dt
              AND (
                    LOWER(COALESCE(sender::text,'')) IN ({email_in_sql})
                 OR EXISTS (
                        SELECT 1 FROM unnest(ARRAY[{email_in_sql}]) AS e(email)
                        WHERE LOWER(COALESCE(receiver::text,'')) LIKE '%' || e.email || '%'
                    )
              )
        ),
        keyed AS (
            SELECT *,
                LOWER(TRIM(COALESCE(sender::text, ''))) AS sender_norm,
                LOWER(TRIM(COALESCE(subject::text, ''))) AS subject_norm,
                LOWER(TRIM(COALESCE(direction::text, ''))) AS direction_norm,
                MD5(LOWER(REGEXP_REPLACE(COALESCE(NULLIF(body::text, ''), NULLIF(snippet::text, ''), subject::text, ''), '\\s+', ' ', 'g'))) AS content_hash,
                (DATE_TRUNC('minute', email_date) - ((EXTRACT(minute FROM email_date)::int % 5) * INTERVAL '1 minute')) AS email_bucket_5m,
                DATE_TRUNC('second', email_date) AS email_second
            FROM matched
        ),
        grouped AS (
            SELECT sender_norm, subject_norm, content_hash, email_bucket_5m,
                COUNT(*) AS duplicate_count,
                MIN(email_date) AS first_email_date,
                MIN(email_second) AS first_email_second,
                MAX(email_second) AS last_email_second,
                COUNT(DISTINCT direction_norm) AS direction_variants
            FROM keyed
            GROUP BY sender_norm, subject_norm, content_hash, email_bucket_5m
        ),
        ranked AS (
            SELECT k.*, g.duplicate_count, g.first_email_date, g.first_email_second, g.last_email_second, g.direction_variants,
                ROW_NUMBER() OVER (
                    PARTITION BY k.sender_norm, k.subject_norm, k.content_hash, k.email_bucket_5m
                    ORDER BY CASE WHEN k.direction_norm IN ('outgoing', 'outbound', 'sent', 'reply', 'from_admin') THEN 0 ELSE 1 END,
                             k.email_date ASC NULLS LAST, k.source_id ASC
                ) AS rn
            FROM keyed k
            JOIN grouped g
              ON g.sender_norm = k.sender_norm
             AND g.subject_norm = k.subject_norm
             AND g.content_hash = k.content_hash
             AND g.email_bucket_5m IS NOT DISTINCT FROM k.email_bucket_5m
        )
        SELECT source_id::text,
            CASE WHEN duplicate_count > 1 AND (first_email_second = last_email_second OR direction_variants > 1)
                 THEN first_email_date ELSE email_date END AS event_time,
            sender, receiver, subject, snippet, body, duplicate_count
        FROM ranked
        WHERE rn = 1 OR NOT (duplicate_count > 1 AND (first_email_second = last_email_second OR direction_variants > 1))
        ORDER BY event_time ASC NULLS LAST, source_id ASC
        LIMIT :limit_n
        """,
        params,
    )

    out: List[Dict[str, Any]] = []
    resolver = role_resolver or StaffRoleResolver(db, schema)
    email_set = set(emails)
    for row in rows:
        sender = norm_email(row.get("sender")) or clean_text(row.get("sender"), 80)
        receiver = clean_text(row.get("receiver"), 160)
        sender_email = norm_email(row.get("sender"))
        inbound = sender_email in email_set
        customer_email = sender_email if inbound else next((email for email in emails if email in str(receiver or "").lower()), emails[0])
        text_value = " | ".join(part for part in [clean_text(row.get("subject"), max_text), clean_text(row.get("snippet") or row.get("body"), max_text)] if part)
        preliminary_role = "sales"
        preliminary_flow = f"customer_to_{preliminary_role}" if inbound else f"{preliminary_role}_to_customer"
        category = classify_text(text_value, preliminary_flow, "text")
        actor_role_fallback = fallback_email_role(sender if not inbound else receiver, category=category, default="sales")
        actor = resolver.resolve(
            email=sender if not inbound else receiver,
            fallback_role=actor_role_fallback,
            fallback_actor=sender if not inbound else receiver,
        )
        actor_role = actor.get("actor_role") or actor_role_fallback
        if category.startswith("auto_") and not inbound:
            actor_role = "system"
            actor = {"actor_role": "system", "actor": "automation"}
        flow = f"customer_to_{actor_role}" if inbound else f"{actor_role}_to_customer"
        priority = priority_for_event("email", flow, category, "text")
        out.append(clean_event_for_mode({
            "time": row.get("event_time"),
            "channel": "email",
            "source_id": row.get("source_id"),
            "flow": flow,
            "direction": "inbound" if inbound else "outbound",
            "actor_role": actor.get("actor_role") or actor_role,
            "actor": actor.get("actor"),
            "actor_source": actor.get("actor_source"),
            "sender": sender,
            "receiver": receiver,
            "customer_email": customer_email,
            "kind": "text",
            "category": category,
            "priority": priority,
            "needs_review": priority in {"P1", "P2"},
            "raw_count": int(row.get("duplicate_count") or 1),
            "text": clean_text(text_value, max_text),
        }, mode="raw"))
    return out


def fetch_site_visit_rows(
    db: Session,
    schema: str,
    lead_id: int,
    start_dt: datetime,
    end_dt: datetime,
    limit: int,
    role_resolver: Optional[StaffRoleResolver] = None,
) -> List[Dict[str, Any]]:
    if not table_exists(db, schema, "staging_site_visits"):
        return []
    rows = q(
        db,
        f"""
        SELECT source_id::text, lead_id::text, executive_id, building_id, prop_id, unit_type,
               schedule_status, visit_type, site_visit_date, added_on
        FROM {schema_ident(schema)}.staging_site_visits
        WHERE lead_id = :lead_id
          AND COALESCE(added_on, site_visit_date) >= :start_dt
          AND COALESCE(added_on, site_visit_date) < :end_dt
        ORDER BY COALESCE(added_on, site_visit_date) ASC NULLS LAST, source_id ASC
        LIMIT :limit_n
        """,
        {"lead_id": lead_id, "start_dt": start_dt, "end_dt": end_dt, "limit_n": int(limit)},
    )
    resolver = role_resolver or StaffRoleResolver(db, schema)
    out: List[Dict[str, Any]] = []
    for row in rows:
        actor = resolver.resolve(username=row.get("executive_id"), fallback_role="sales", fallback_actor=row.get("executive_id"))
        actor_role = actor.get("actor_role") or "sales"
        out.append(clean_event_for_mode({
            "time": row.get("added_on") or row.get("site_visit_date"),
            "channel": "site_visit",
            "source_id": row.get("source_id"),
            "lead_id": row.get("lead_id"),
            "flow": "customer_activity",
            "status": str(row.get("schedule_status") or ""),
            "actor_role": actor_role,
            "actor": actor.get("actor"),
            "actor_source": actor.get("actor_source"),
            "visit_at": row.get("site_visit_date"),
            "created_at": row.get("added_on"),
            "property_id": row.get("prop_id"),
            "building_id": row.get("building_id"),
            "unit_type": row.get("unit_type"),
            "visit_type": row.get("visit_type"),
            "kind": "site_visit",
            "category": "lead_site_visit",
            "priority": "P3",
            "needs_review": False,
            "text": clean_text(f"Site visit created={fmt_dt(row.get('added_on'))} visit_at={fmt_dt(row.get('site_visit_date'))} property={row.get('prop_id') or '-'} building={row.get('building_id') or '-'} unit_type={row.get('unit_type') or '-'}", 220),
        }, mode="raw"))
    return out


def fetch_booking_confirm_rows(db: Session, schema: str, lead_id: int, contacts: Dict[str, Any], start_dt: datetime, end_dt: datetime, limit: int, max_text: int = 220) -> List[Dict[str, Any]]:
    if not table_exists(db, schema, "staging_booking_confirm"):
        return []
    user_ids = normalized_id_list(contacts.get("user_ids") or contacts.get("user_id"))
    booking_ids = normalized_id_list(contacts.get("booking_ids") or contacts.get("booking_id"))

    conds = ["lead_id::text = :lead_id_text"]
    params: Dict[str, Any] = {"lead_id_text": str(lead_id), "start_dt": start_dt, "end_dt": end_dt, "limit_n": int(limit)}
    if user_ids:
        holders = []
        for idx, user_id in enumerate(user_ids):
            key = f"bcu{idx}"
            holders.append(f":{key}")
            params[key] = str(user_id)
        conds.append("user_id::text IN (" + ", ".join(holders) + ")")
    if booking_ids:
        holders = []
        for idx, booking_id in enumerate(booking_ids):
            key = f"bcb{idx}"
            holders.append(f":{key}")
            params[key] = str(booking_id)
        joined = ", ".join(holders)
        conds.append(f"(booking_id::text IN ({joined}) OR source_id::text IN ({joined}))")

    rows = q(
        db,
        f"""
        SELECT source_id::text, booking_id::text, lead_id::text, user_id::text, prop_id,
               booking_status, host_confirm_status, booking_type, period,
               booking_datetime, travel_from_date, travel_to_date, nights,
               total_amount, total_after_discount, amount_paid, advance_amount, paid_advance_amount, synced_at
        FROM {schema_ident(schema)}.staging_booking_confirm
        WHERE ({' OR '.join(f'({cond})' for cond in conds)})
          AND COALESCE(booking_datetime, synced_at, travel_from_date) >= :start_dt
          AND COALESCE(booking_datetime, synced_at, travel_from_date) < :end_dt
        ORDER BY COALESCE(booking_datetime, synced_at, travel_from_date) ASC NULLS LAST, source_id ASC
        LIMIT :limit_n
        """,
        params,
    )

    out: List[Dict[str, Any]] = []
    for row in rows:
        booking_id = row.get("booking_id") or row.get("source_id")
        status = row.get("booking_status") or row.get("host_confirm_status")
        text_value = (
            f"Booked booking_id={booking_id} property={row.get('prop_id') or '-'}"
            f" stay={fmt_dt(row.get('travel_from_date')) or '-'}→{fmt_dt(row.get('travel_to_date')) or '-'}"
            f" nights={row.get('nights') or '-'} type={row.get('booking_type') or row.get('period') or '-'}"
            f" amount={row.get('total_after_discount') or row.get('total_amount') or '-'}"
            f" paid={row.get('amount_paid') or row.get('paid_advance_amount') or '-'}"
            f" advance={row.get('advance_amount') or '-'} status={status or '-'}"
        )
        out.append(clean_event_for_mode({
            "time": row.get("booking_datetime") or row.get("synced_at") or row.get("travel_from_date"),
            "channel": "booking",
            "source_id": row.get("source_id"),
            "lead_id": row.get("lead_id"),
            "user_id": row.get("user_id"),
            "flow": "customer_activity",
            "status": str(status or ""),
            "booking_id": str(booking_id) if booking_id not in (None, "") else None,
            "booking_status": str(status or "") if status not in (None, "") else None,
            "property_id": row.get("prop_id"),
            "prop_id": row.get("prop_id"),
            "travel_from_date": row.get("travel_from_date"),
            "travel_to_date": row.get("travel_to_date"),
            "nights": row.get("nights"),
            "booking_type": row.get("booking_type") or row.get("period"),
            "total_amount": row.get("total_after_discount") or row.get("total_amount"),
            "amount_paid": row.get("amount_paid") or row.get("paid_advance_amount"),
            "advance_amount": row.get("advance_amount"),
            "text": clean_text(text_value, max_text),
        }, mode="raw"))
    return out


def fetch_travel_cart_rows(
    db: Session,
    schema: str,
    user_id: Any,
    start_dt: datetime,
    end_dt: datetime,
    limit: int,
    max_text: int = 220,
) -> List[Dict[str, Any]]:
    user_ids = normalized_id_list(user_id)
    if not user_ids or not table_exists(db, schema, "staging_travel_cart"):
        return []

    property_join_sql = ""
    property_name_select = "NULL AS property_name"

    if table_exists(db, schema, "staging_property_unit"):
        property_columns = table_columns(db, schema, "staging_property_unit")

        if "unit_name" in property_columns:
            property_name_select = "NULLIF(TRIM(pu.unit_name::text), '') AS property_name"

            if "prop_id" in property_columns:
                property_join_sql = (
                    f"LEFT JOIN {schema_ident(schema)}.staging_property_unit pu "
                    f"ON pu.prop_id::text = tc.prop_id::text"
                )
            elif "source_id" in property_columns:
                property_join_sql = (
                    f"LEFT JOIN {schema_ident(schema)}.staging_property_unit pu "
                    f"ON pu.source_id::text = tc.prop_id::text"
                )

    holders = []
    params: Dict[str, Any] = {"start_dt": start_dt, "end_dt": end_dt, "limit_n": int(limit)}

    for idx, uid in enumerate(user_ids):
        key = f"tc_uid_{idx}"
        holders.append(f":{key}")
        params[key] = str(uid)

    rows = q(
        db,
        f"""
        SELECT
            tc.source_id::text,
            tc.user_id::text,
            tc.prop_id,
            {property_name_select},
            tc.travel_from_date,
            tc.travel_to_date,
            tc.nights,
            tc.booking_type,
            tc.total_amount,
            tc.advance_amount,
            tc.pending_amount,
            tc.source,
            tc.added_on,
            tc.bkc_status
        FROM {schema_ident(schema)}.staging_travel_cart tc
        {property_join_sql}
        WHERE tc.user_id::text IN ({', '.join(holders)})
          AND tc.added_on >= :start_dt
          AND tc.added_on < :end_dt
        ORDER BY tc.added_on ASC NULLS LAST, tc.source_id ASC
        LIMIT :limit_n
        """,
        params,
    )

    out: List[Dict[str, Any]] = []

    for row in rows:
        property_name = row.get("property_name") or "-"

        text_value = (
            f"Booking attempt property_name={property_name}"
            f" stay={fmt_dt(row.get('travel_from_date')) or '-'}→{fmt_dt(row.get('travel_to_date')) or '-'}"
            f" nights={row.get('nights') or '-'} type={row.get('booking_type') or '-'}"
            f" amount={row.get('total_amount') or '-'} advance={row.get('advance_amount') or '-'}"
            f" pending={row.get('pending_amount') or '-'} source={row.get('source') or '-'}"
            f" status={row.get('bkc_status') if row.get('bkc_status') not in (None, '') else '-'}"
        )

        priority = priority_for_event(
            "travel_cart",
            "customer_activity",
            "lead_booking_attempt",
            "booking_attempt",
        )

        out.append(clean_event_for_mode({
            "time": row.get("added_on"),
            "channel": "travel_cart",
            "source_id": row.get("source_id"),
            "user_id": row.get("user_id"),
            "flow": "customer_activity",
            "status": str(row.get("bkc_status") or ""),
            "property_name": row.get("property_name"),
            "kind": "booking_attempt",
            "category": "lead_booking_attempt",
            "priority": priority,
            "needs_review": True,
            "match_by": f"user_id={row.get('user_id')}" if row.get("user_id") not in (None, "") else None,
            "text": clean_text(text_value, max_text),
            "travel_from_date": row.get("travel_from_date"),
            "travel_to_date": row.get("travel_to_date"),
            "nights": row.get("nights"),
            "booking_type": row.get("booking_type"),
            "total_amount": row.get("total_amount"),
            "advance_amount": row.get("advance_amount"),
            "pending_amount": row.get("pending_amount"),
            "cart_source": row.get("source"),
        }, mode="raw"))

    return out
