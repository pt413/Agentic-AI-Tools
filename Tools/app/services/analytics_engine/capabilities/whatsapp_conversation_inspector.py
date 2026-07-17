#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "..", "..", "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.services.analytics_engine.core.utils import (
    build_whatsapp_conversation_key,
    build_whatsapp_conversation_summary,
    build_whatsapp_message_preview,
    build_whatsapp_turns,
    detect_whatsapp_message_ontology,
    humanize_label,
    normalize_phone,
)

DEFAULT_SCHEMA = "AnalyticsEngine"
DEFAULT_LIMIT = 120
DEFAULT_THREAD_ROW_LIMIT = 5000
DEFAULT_OVERALL_EVENT_LIMIT = 500

PROPERTY_SHARE_MARKERS = (
    "no brokerage",
    "building name",
    "caretaker number",
    "click for photos",
    "click for booking",
    "fully furnished",
    "semi furnished",
    "unfurnished",
    "rent",
    "deposit",
    "1bhk",
    "2bhk",
    "3bhk",
    "4bhk",
    "studio",
)

PROPERTY_SHARE_REFUSAL_PHRASES = (
    "not available",
    "no availability",
    "already booked",
    "already occupied",
    "fully occupied",
    "no vacancy",
    "unavailable",
    "sold out",
    "cannot provide",
    "can't provide",
    "unable to provide",
    "unable to share",
    "cannot share",
)

PAYMENT_REFERENCE_MARKERS = (
    "utr",
    "upi",
    "transaction id",
    "txn id",
    "payment received",
    "paid via",
    "paid using",
    "receipt",
    "invoice",
)

EVENT_TOPIC_FALLBACK = {
    "support": "Support",
    "finance": "Finance",
    "booking": "Booking",
    "stay": "Stay Experience",
    "lead": "Lead / Discovery",
    "identity": "Account / Identity",
    "communication": "General Communication",
}

CHANNEL_TOPIC_FALLBACK = {
    "whatsapp": "WhatsApp Communication",
    "email": "Email Communication",
    "call": "Call Communication",
    "sms": "SMS Communication",
    "ticket": "Support",
    "booking": "Booking",
}


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _normalize_ontology_text(value):
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def _looks_like_property_share_message(clean_content, message_type, direction):
    text = _normalize_ontology_text(clean_content)
    if not text:
        return False

    msg_type = _normalize_ontology_text(message_type)
    msg_direction = _normalize_ontology_text(direction)

    if msg_type not in {"", "text", "template", "extendedtextmessage"}:
        return False

    if msg_direction and msg_direction not in {"outbound", "outgoing", "sent", "reply", "from_admin"}:
        return False

    if any(phrase in text for phrase in PROPERTY_SHARE_REFUSAL_PHRASES):
        return False

    marker_hits = sum(1 for marker in PROPERTY_SHARE_MARKERS if marker in text)

    has_bhk = bool(re.search(r"\b[1-9]\s*bhk\b", text))
    has_rent = "rent" in text
    has_deposit = "deposit" in text
    has_caretaker = "caretaker number" in text
    has_building = "building name" in text
    has_listing_link = ("click for photos" in text) or ("click for booking" in text)

    if marker_hits >= 3:
        return True

    if has_building and (has_rent or has_deposit):
        return True

    if has_bhk and (has_rent or has_deposit) and (has_caretaker or has_listing_link):
        return True

    return False


def _apply_property_share_guard(ontology, *, clean_content, message_type, direction):
    if not _looks_like_property_share_message(clean_content, message_type, direction):
        return ontology

    text = _normalize_ontology_text(clean_content)
    msg_type = _normalize_ontology_text(message_type)

    guarded = dict(ontology or {})
    guarded.update(
        {
            "message_kind": "template" if msg_type == "template" else "text",
            "speech_act": "share_information",
            "topic_primary": "location_property",
            "intent_primary": "share_location",
            "journey_stage": "pre_commitment",
            "resolution_stage": "informational",
            "urgency": "medium",
            "requires_reply": True,
            "is_actionable": True,
            "contains_amount": ("rent" in text or "deposit" in text or bool(re.search(r"\b\d{4,}\b", text))),
            "contains_payment_reference": any(marker in text for marker in PAYMENT_REFERENCE_MARKERS),
            "contains_location_reference": (
                "building name" in text
                or "location" in text
                or bool(re.search(r"\b(?:1|2|3|4)\s*bhk\b", text))
            ),
            "tagged_from": "rule_engine_v2_message_property_guard",
            "confidence": {
                "channel": 0.99,
                "direction": 0.9,
                "message_kind": 0.95,
                "speech_act": 0.96,
                "topic_primary": 0.96,
                "intent_primary": 0.95,
                "journey_stage": 0.82,
                "resolution_stage": 0.9,
                "urgency": 0.72,
            },
        }
    )
    return guarded


def _try_load_env():
    if load_dotenv is None:
        return

    candidates = [
        Path(PROJECT_ROOT) / ".env",
        Path(PROJECT_ROOT).parent / ".env",
        Path.cwd() / ".env",
    ]
    for env_path in candidates:
        if env_path.exists():
            load_dotenv(env_path, override=False)


@contextmanager
def get_session() -> Iterable[Session]:
    """Open a DB session without swallowing exceptions raised inside the with-block."""
    repo_gen = None
    repo_db = None

    try:
        from app.db.database import get_db  # type: ignore

        repo_gen = get_db()
        repo_db = next(repo_gen)
        repo_db.execute(text("SELECT 1"))
    except Exception:
        if repo_db is not None:
            try:
                repo_db.close()
            except Exception:
                pass
        if repo_gen is not None:
            try:
                repo_gen.close()
            except Exception:
                pass
        repo_gen = None
        repo_db = None

    if repo_db is not None:
        try:
            yield repo_db
        finally:
            try:
                repo_db.close()
            except Exception:
                pass
            try:
                repo_gen.close()  # type: ignore[union-attr]
            except Exception:
                pass
        return

    _try_load_env()
    url = os.getenv("DATABASE_URL") or os.getenv("PG_URL")
    if not url:
        raise RuntimeError("No DB available. Run inside repo or set DATABASE_URL / PG_URL.")

    engine = create_engine(url, pool_pre_ping=True)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        try:
            db.close()
        finally:
            engine.dispose()

def q(db: Session, sql: str, params: Optional[dict] = None) -> List[Dict[str, Any]]:
    rows = db.execute(text(sql), params or {}).fetchall()
    return [dict(r._mapping) for r in rows]


def q1(db: Session, sql: str, params: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    row = db.execute(text(sql), params or {}).fetchone()
    return dict(row._mapping) if row else None


def build_named_in_params(values: List[Any], prefix: str) -> Tuple[str, Dict[str, Any]]:
    params: Dict[str, Any] = {}
    placeholders: List[str] = []
    for idx, value in enumerate(values):
        key = f"{prefix}_{idx}"
        placeholders.append(f":{key}")
        params[key] = value
    if not placeholders:
        return "(NULL)", {}
    return f"({', '.join(placeholders)})", params


def phone_last10(value: Optional[str]) -> Optional[str]:
    normalized = normalize_phone(value)
    if not normalized:
        return None
    return normalized[-10:]


def fmt_dt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def fmt_value(value: Any) -> str:
    if value is None:
        return "-"
    return str(value)


def parse_jsonish(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}
    return {}


def normalize_direction(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in {"outgoing", "outbound", "sent", "reply", "from_admin"}:
        return "outbound"
    if text in {"incoming", "inbound", "received", "from_customer"}:
        return "inbound"
    return text


def normalize_remote_jid(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def infer_conversation_kind(*, remote_jid=None, customer_phone=None, admin_phone=None) -> str:
    jid = normalize_remote_jid(remote_jid)
    if jid:
        lowered = jid.lower()
        if lowered.endswith("@g.us"):
            return "group"
        if lowered.endswith("@s.whatsapp.net") or lowered.endswith("@lid"):
            return "direct"

    if normalize_phone(customer_phone) and normalize_phone(admin_phone):
        return "direct"
    return "unknown"


def build_thread_key(*, customer_phone=None, admin_phone=None, remote_jid=None) -> Optional[str]:
    return build_whatsapp_conversation_key(
        customer_phone=normalize_phone(customer_phone),
        admin_phone=normalize_phone(admin_phone),
        remote_jid=normalize_remote_jid(remote_jid),
    )


def parse_thread_key(value: Optional[str]) -> Dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}

    lowered = text.lower()
    if lowered.startswith("jid:"):
        remote_jid = text[4:].strip()
        return {
            "thread_key": text,
            "remote_jid": remote_jid,
            "admin_number": None,
            "customer_number": None,
            "conversation_kind": "group",
        }

    if lowered.startswith("wa:") and "->" in text:
        body = text[3:]
        admin_number, customer_number = body.split("->", 1)
        admin_number = normalize_phone(admin_number)
        customer_number = normalize_phone(customer_number)
        return {
            "thread_key": build_thread_key(customer_phone=customer_number, admin_phone=admin_number),
            "remote_jid": None,
            "admin_number": admin_number,
            "customer_number": customer_number,
            "conversation_kind": "direct",
        }

    if text.endswith("@g.us") or text.endswith("@s.whatsapp.net"):
        return {
            "thread_key": build_thread_key(remote_jid=text),
            "remote_jid": text,
            "admin_number": None,
            "customer_number": None,
            "conversation_kind": infer_conversation_kind(remote_jid=text),
        }

    if "->" in text:
        admin_number, customer_number = text.split("->", 1)
        admin_number = normalize_phone(admin_number)
        customer_number = normalize_phone(customer_number)
        return {
            "thread_key": build_thread_key(customer_phone=customer_number, admin_phone=admin_number),
            "remote_jid": None,
            "admin_number": admin_number,
            "customer_number": customer_number,
            "conversation_kind": "direct",
        }

    return {}


def has_explicit_thread_selector(args: argparse.Namespace) -> bool:
    return any(
        [
            getattr(args, "thread_key", None),
            getattr(args, "remote_jid", None),
            getattr(args, "admin_number", None) and getattr(args, "customer_number", None),
        ]
    )


def resolve_exact_thread_filters(args: argparse.Namespace) -> Dict[str, Any]:
    parsed = parse_thread_key(getattr(args, "thread_key", None))
    remote_jid = normalize_remote_jid(getattr(args, "remote_jid", None) or parsed.get("remote_jid"))
    admin_number = normalize_phone(getattr(args, "admin_number", None) or parsed.get("admin_number"))
    customer_number = normalize_phone(getattr(args, "customer_number", None) or parsed.get("customer_number"))
    thread_key = build_thread_key(
        customer_phone=customer_number,
        admin_phone=admin_number,
        remote_jid=remote_jid,
    )
    return {
        "thread_key": thread_key,
        "remote_jid": remote_jid,
        "admin_number": admin_number,
        "customer_number": customer_number,
        "conversation_kind": infer_conversation_kind(
            remote_jid=remote_jid,
            customer_phone=customer_number,
            admin_phone=admin_number,
        ),
    }


def build_search_conditions(args: argparse.Namespace) -> tuple[str, dict]:
    clauses: List[str] = []
    params: Dict[str, Any] = {}
    explicit = resolve_exact_thread_filters(args)

    if explicit.get("remote_jid"):
        clauses.append("COALESCE(sw.remote_jid,'') = :remote_jid")
        params["remote_jid"] = explicit["remote_jid"]
    elif explicit.get("admin_number") and explicit.get("customer_number"):
        clauses.append("RIGHT(REGEXP_REPLACE(COALESCE(sw.admin_number,''), '\\D', '', 'g'), 10) = :admin10")
        clauses.append("RIGHT(REGEXP_REPLACE(COALESCE(sw.cx_number,''), '\\D', '', 'g'), 10) = :customer10")
        params["admin10"] = explicit["admin_number"][-10:]
        params["customer10"] = explicit["customer_number"][-10:]
    else:
        if args.phone:
            params["phone10"] = phone_last10(args.phone)
            clauses.append(
                "("
                "RIGHT(REGEXP_REPLACE(COALESCE(sw.cx_number,''), '\\D', '', 'g'), 10) = :phone10 "
                "OR RIGHT(REGEXP_REPLACE(COALESCE(sw.admin_number,''), '\\D', '', 'g'), 10) = :phone10"
                ")"
            )

        if args.customer_number:
            params["customer10"] = phone_last10(args.customer_number)
            clauses.append("RIGHT(REGEXP_REPLACE(COALESCE(sw.cx_number,''), '\\D', '', 'g'), 10) = :customer10")

        if args.admin_number:
            params["admin10"] = phone_last10(args.admin_number)
            clauses.append("RIGHT(REGEXP_REPLACE(COALESCE(sw.admin_number,''), '\\D', '', 'g'), 10) = :admin10")

    if args.actor:
        params["actor"] = str(args.actor).strip().lower()
        clauses.append("LOWER(COALESCE(sw.executive_id,'')) = :actor")

    if args.lead_id is not None:
        params["lead_id"] = int(args.lead_id)
        clauses.append("sw.lead_id = :lead_id")

    if not clauses:
        raise ValueError(
            "Provide at least one of --phone, --actor, --lead-id, --thread-key, --remote-jid, or --admin-number/--customer-number"
        )

    return " AND ".join(clauses), params


def fetch_person_phones(db: Session, schema: str, person_ids: List[int], fallback_phone: Optional[str] = None) -> List[str]:
    phones: List[str] = []
    seen = set()

    if person_ids:
        in_sql, in_params = build_named_in_params(person_ids, "pid")
        rows = q(
            db,
            f'''
            SELECT DISTINCT key_value
            FROM "{schema}".identity_person_key
            WHERE person_id IN {in_sql}
              AND key_type = 'phone'
            ''',
            in_params,
        )
        for row in rows:
            p10 = phone_last10(row.get("key_value"))
            if p10 and p10 not in seen:
                seen.add(p10)
                phones.append(p10)

    fallback10 = phone_last10(fallback_phone)
    if fallback10 and fallback10 not in seen:
        phones.append(fallback10)

    return phones


def resolve_anchor(db: Session, schema: str, args: argparse.Namespace) -> dict:
    explicit = resolve_exact_thread_filters(args)
    where_sql, params = build_search_conditions(args)
    row = q1(
        db,
        f'''
        SELECT
            sw.source_id,
            sw.message_time,
            sw.lead_id,
            sw.remote_jid,
            sw.admin_number,
            sw.cx_number,
            sw.executive_id
        FROM "{schema}".staging_whatsapp_messages sw
        WHERE {where_sql}
        ORDER BY sw.message_time DESC NULLS LAST, CAST(sw.source_id AS TEXT) DESC
        LIMIT 1
        ''',
        params,
    )
    if not row:
        admin_number = explicit.get("admin_number") or normalize_phone(args.admin_number)
        customer_number = explicit.get("customer_number") or normalize_phone(args.customer_number or args.phone)
        remote_jid = explicit.get("remote_jid")
        thread_key = explicit.get("thread_key") or build_thread_key(
            customer_phone=customer_number,
            admin_phone=admin_number,
            remote_jid=remote_jid,
        )
        conversation_kind = explicit.get("conversation_kind") or infer_conversation_kind(
            remote_jid=remote_jid,
            customer_phone=customer_number,
            admin_phone=admin_number,
        )
        return {
            "person_ids": [],
            "lead_id": args.lead_id,
            "remote_jid": remote_jid,
            "admin_number": admin_number,
            "customer_number": customer_number,
            "executive_id": args.actor,
            "source_id": None,
            "message_time": None,
            "thread_key": thread_key,
            "conversation_key": thread_key,
            "conversation_kind": conversation_kind,
        }

    customer_number = normalize_phone(row.get("cx_number"))
    admin_number = normalize_phone(row.get("admin_number"))
    remote_jid = normalize_remote_jid(row.get("remote_jid"))
    thread_key = build_thread_key(
        customer_phone=customer_number,
        admin_phone=admin_number,
        remote_jid=remote_jid,
    )
    conversation_kind = infer_conversation_kind(
        remote_jid=remote_jid,
        customer_phone=customer_number,
        admin_phone=admin_number,
    )

    person_ids: List[int] = []
    person_phone = customer_number or normalize_phone(args.phone)
    if person_phone:
        rows = q(
            db,
            f'''
            SELECT DISTINCT person_id
            FROM "{schema}".identity_person_key
            WHERE key_type = 'phone'
              AND RIGHT(REGEXP_REPLACE(COALESCE(key_value,''), '\\D', '', 'g'), 10) = :phone10
            ORDER BY person_id
            ''',
            {"phone10": person_phone[-10:]},
        )
        person_ids = [int(r["person_id"]) for r in rows if r.get("person_id") is not None]

    return {
        "person_ids": person_ids,
        "lead_id": row.get("lead_id"),
        "remote_jid": remote_jid,
        "admin_number": admin_number,
        "customer_number": customer_number,
        "executive_id": row.get("executive_id"),
        "source_id": row.get("source_id"),
        "message_time": row.get("message_time"),
        "thread_key": thread_key,
        "conversation_key": thread_key,
        "conversation_kind": conversation_kind,
    }


def build_conversation_where(anchor: dict, args: argparse.Namespace) -> tuple[str, dict]:
    clauses: List[str] = []
    params: Dict[str, Any] = {}
    explicit = resolve_exact_thread_filters(args)

    remote_jid = explicit.get("remote_jid") or anchor.get("remote_jid")
    admin_number = explicit.get("admin_number") or anchor.get("admin_number")
    customer_number = explicit.get("customer_number") or anchor.get("customer_number")

    if remote_jid:
        clauses.append("COALESCE(sw.remote_jid,'') = :remote_jid")
        params["remote_jid"] = remote_jid
    else:
        customer10 = phone_last10(customer_number)
        admin10 = phone_last10(admin_number)
        if customer10:
            clauses.append("RIGHT(REGEXP_REPLACE(COALESCE(sw.cx_number,''), '\\D', '', 'g'), 10) = :customer10")
            params["customer10"] = customer10
        if admin10:
            clauses.append("RIGHT(REGEXP_REPLACE(COALESCE(sw.admin_number,''), '\\D', '', 'g'), 10) = :admin10")
            params["admin10"] = admin10

    if args.lead_id is not None:
        clauses.append("sw.lead_id = :lead_id")
        params["lead_id"] = int(args.lead_id)

    if not clauses and args.phone:
        clauses.append(
            "("
            "RIGHT(REGEXP_REPLACE(COALESCE(sw.cx_number,''), '\\D', '', 'g'), 10) = :phone10 "
            "OR RIGHT(REGEXP_REPLACE(COALESCE(sw.admin_number,''), '\\D', '', 'g'), 10) = :phone10"
            ")"
        )
        params["phone10"] = phone_last10(args.phone)

    if not clauses:
        raise ValueError("Unable to build conversation filter from the provided anchor")

    return " AND ".join(clauses), params


def _derive_row_ontology(row: dict) -> dict:
    event_meta = parse_jsonish(row.get("event_meta"))
    ontology = event_meta.get("message_ontology")
    if not isinstance(ontology, dict) or not ontology:
        ontology = detect_whatsapp_message_ontology(
            clean_content=row.get("clean_content"),
            direction=row.get("event_direction") or row.get("direction"),
            message_type=row.get("message_type"),
            remote_jid=row.get("remote_jid"),
        )

    ontology = _apply_property_share_guard(
        ontology,
        clean_content=row.get("clean_content") or event_meta.get("content_preview"),
        message_type=row.get("message_type") or event_meta.get("message_type"),
        direction=row.get("event_direction") or row.get("direction") or event_meta.get("direction"),
    )

    conversation_kind = event_meta.get("conversation_kind") or infer_conversation_kind(
        remote_jid=row.get("remote_jid"),
        customer_phone=row.get("cx_number"),
        admin_phone=row.get("admin_number"),
    )
    if conversation_kind and ontology.get("conversation_kind") in (None, "", "unknown"):
        ontology["conversation_kind"] = conversation_kind

    return ontology


def fetch_messages(db: Session, schema: str, anchor: dict, args: argparse.Namespace) -> List[Dict[str, Any]]:
    where_sql, params = build_conversation_where(anchor, args)
    params["limit_n"] = int(args.limit or DEFAULT_LIMIT)

    rows = q(
        db,
        f'''
        SELECT
            sw.source_id,
            sw.message_time,
            sw.lead_id,
            sw.remote_jid,
            sw.admin_number,
            sw.cx_number,
            sw.executive_id,
            sw.direction,
            sw.message_type,
            sw.clean_content,
            sw.isread,
            sw.issent,
            sw.synced_at,
            ef.event_id,
            ef.event_name,
            ef.event_family,
            ef.event_channel,
            ef.event_direction,
            ef.event_status,
            ef.event_meta
        FROM "{schema}".staging_whatsapp_messages sw
        LEFT JOIN "{schema}".event_fact ef
          ON ef.source_table = 'staging_whatsapp_messages'
         AND ef.source_id = CAST(sw.source_id AS TEXT)
        WHERE {where_sql}
        ORDER BY sw.message_time ASC NULLS LAST, CAST(sw.source_id AS TEXT) ASC
        LIMIT :limit_n
        ''',
        params,
    )

    messages: List[Dict[str, Any]] = []
    for row in rows:
        event_meta = parse_jsonish(row.get("event_meta"))
        preview = event_meta.get("content_preview") or build_whatsapp_message_preview(
            clean_content=row.get("clean_content"),
            message_type=row.get("message_type"),
        )
        message_ontology = _derive_row_ontology(row)
        thread_key = (
            event_meta.get("thread_key")
            or event_meta.get("conversation_key")
            or build_thread_key(
                customer_phone=row.get("cx_number"),
                admin_phone=row.get("admin_number"),
                remote_jid=row.get("remote_jid"),
            )
        )
        conversation_kind = event_meta.get("conversation_kind") or message_ontology.get("conversation_kind") or infer_conversation_kind(
            remote_jid=row.get("remote_jid"),
            customer_phone=row.get("cx_number"),
            admin_phone=row.get("admin_number"),
        )

        messages.append(
            {
                **row,
                "event_meta": event_meta,
                "preview": preview,
                "message_ontology": message_ontology,
                "thread_key": thread_key,
                "conversation_key": thread_key,
                "conversation_kind": conversation_kind,
            }
        )

    return messages


def build_thread_list_where(db: Session, schema: str, anchor: dict, args: argparse.Namespace) -> tuple[str, dict]:
    explicit = resolve_exact_thread_filters(args)
    clauses: List[str] = []
    params: Dict[str, Any] = {}

    if explicit.get("remote_jid"):
        clauses.append("COALESCE(sw.remote_jid,'') = :remote_jid")
        params["remote_jid"] = explicit["remote_jid"]
    elif explicit.get("admin_number") and explicit.get("customer_number"):
        clauses.append("RIGHT(REGEXP_REPLACE(COALESCE(sw.admin_number,''), '\\D', '', 'g'), 10) = :admin10")
        clauses.append("RIGHT(REGEXP_REPLACE(COALESCE(sw.cx_number,''), '\\D', '', 'g'), 10) = :customer10")
        params["admin10"] = explicit["admin_number"][-10:]
        params["customer10"] = explicit["customer_number"][-10:]
    else:
        phone_candidates = fetch_person_phones(
            db,
            schema,
            anchor.get("person_ids") or [],
            fallback_phone=args.phone or anchor.get("customer_number") or args.customer_number,
        )
        if phone_candidates:
            in_sql, in_params = build_named_in_params(phone_candidates, "thread_phone")
            clauses.append(
                "("
                f"RIGHT(REGEXP_REPLACE(COALESCE(sw.cx_number,''), '\\D', '', 'g'), 10) IN {in_sql} "
                f"OR RIGHT(REGEXP_REPLACE(COALESCE(sw.admin_number,''), '\\D', '', 'g'), 10) IN {in_sql}"
                ")"
            )
            params.update(in_params)
        elif args.phone:
            params["phone10"] = phone_last10(args.phone)
            clauses.append(
                "("
                "RIGHT(REGEXP_REPLACE(COALESCE(sw.cx_number,''), '\\D', '', 'g'), 10) = :phone10 "
                "OR RIGHT(REGEXP_REPLACE(COALESCE(sw.admin_number,''), '\\D', '', 'g'), 10) = :phone10"
                ")"
            )

    if args.actor:
        params["actor"] = str(args.actor).strip().lower()
        clauses.append("LOWER(COALESCE(sw.executive_id,'')) = :actor")

    if args.lead_id is not None:
        params["lead_id"] = int(args.lead_id)
        clauses.append("sw.lead_id = :lead_id")

    if not clauses:
        raise ValueError("Unable to build thread filter from the provided input")

    return " AND ".join(clauses), params


def _row_sort_key(row: dict):
    return (
        row.get("message_time") or datetime.min,
        str(row.get("source_id") or ""),
    )


def fetch_threads(db: Session, schema: str, anchor: dict, args: argparse.Namespace) -> List[Dict[str, Any]]:
    where_sql, params = build_thread_list_where(db, schema, anchor, args)
    params["limit_n"] = int(max((args.limit or DEFAULT_LIMIT) * 100, DEFAULT_THREAD_ROW_LIMIT))

    rows = q(
        db,
        f'''
        SELECT
            sw.source_id,
            sw.message_time,
            sw.lead_id,
            sw.remote_jid,
            sw.admin_number,
            sw.cx_number,
            sw.executive_id,
            sw.direction,
            sw.message_type,
            sw.clean_content,
            ef.event_direction,
            ef.event_meta
        FROM "{schema}".staging_whatsapp_messages sw
        LEFT JOIN "{schema}".event_fact ef
          ON ef.source_table = 'staging_whatsapp_messages'
         AND ef.source_id = CAST(sw.source_id AS TEXT)
        WHERE {where_sql}
        ORDER BY sw.message_time ASC NULLS LAST, CAST(sw.source_id AS TEXT) ASC
        LIMIT :limit_n
        ''',
        params,
    )

    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        event_meta = parse_jsonish(row.get("event_meta"))
        thread_key = (
            event_meta.get("thread_key")
            or event_meta.get("conversation_key")
            or build_thread_key(
                customer_phone=row.get("cx_number"),
                admin_phone=row.get("admin_number"),
                remote_jid=row.get("remote_jid"),
            )
        )
        if not thread_key:
            continue

        conversation_kind = event_meta.get("conversation_kind") or infer_conversation_kind(
            remote_jid=row.get("remote_jid"),
            customer_phone=row.get("cx_number"),
            admin_phone=row.get("admin_number"),
        )
        direction = normalize_direction(row.get("event_direction") or row.get("direction"))
        preview = event_meta.get("content_preview") or build_whatsapp_message_preview(
            clean_content=row.get("clean_content"),
            message_type=row.get("message_type"),
        )
        ontology = _derive_row_ontology(row)

        thread = grouped.get(thread_key)
        if thread is None:
            thread = {
                "thread_key": thread_key,
                "conversation_kind": conversation_kind,
                "remote_jid": normalize_remote_jid(row.get("remote_jid")),
                "admin_number": normalize_phone(row.get("admin_number")),
                "customer_number": normalize_phone(row.get("cx_number")),
                "lead_id": row.get("lead_id"),
                "message_count": 0,
                "inbound_messages": 0,
                "outbound_messages": 0,
                "first_message_time": row.get("message_time"),
                "last_message_time": row.get("message_time"),
                "latest_preview": preview,
                "latest_topic": humanize_label(ontology.get("topic_primary")),
                "latest_intent": humanize_label(ontology.get("intent_primary")),
                "latest_direction": direction,
                "latest_source_id": row.get("source_id"),
            }
            grouped[thread_key] = thread

        thread["message_count"] += 1
        if direction == "inbound":
            thread["inbound_messages"] += 1
        elif direction == "outbound":
            thread["outbound_messages"] += 1

        if row.get("message_time") and (
            thread.get("first_message_time") is None or row.get("message_time") < thread.get("first_message_time")
        ):
            thread["first_message_time"] = row.get("message_time")

        if _row_sort_key(row) >= _row_sort_key({
            "message_time": thread.get("last_message_time"),
            "source_id": thread.get("latest_source_id"),
        }):
            thread["last_message_time"] = row.get("message_time")
            thread["latest_preview"] = preview
            thread["latest_topic"] = humanize_label(ontology.get("topic_primary"))
            thread["latest_intent"] = humanize_label(ontology.get("intent_primary"))
            thread["latest_direction"] = direction
            thread["latest_source_id"] = row.get("source_id")
            thread["lead_id"] = row.get("lead_id") or thread.get("lead_id")
            thread["remote_jid"] = normalize_remote_jid(row.get("remote_jid")) or thread.get("remote_jid")
            thread["admin_number"] = normalize_phone(row.get("admin_number")) or thread.get("admin_number")
            thread["customer_number"] = normalize_phone(row.get("cx_number")) or thread.get("customer_number")
            thread["conversation_kind"] = conversation_kind or thread.get("conversation_kind")

    threads = list(grouped.values())
    threads.sort(
        key=lambda item: (
            item.get("last_message_time") or datetime.min,
            str(item.get("thread_key") or ""),
        ),
        reverse=True,
    )
    return threads


def fetch_latest_participants(db: Session, schema: str, latest_event_id: Optional[int], latest_row: Optional[dict]) -> List[Dict[str, Any]]:
    if latest_event_id:
        rows = q(
            db,
            f'''
            SELECT
                participant_seq,
                participant_role,
                direction_role,
                raw_key_type,
                raw_key_value,
                person_id
            FROM "{schema}".event_participant
            WHERE event_id = :event_id
            ORDER BY participant_seq
            ''',
            {"event_id": int(latest_event_id)},
        )
        if rows:
            return rows

    participants: List[Dict[str, Any]] = []
    if latest_row:
        customer_phone = normalize_phone(latest_row.get("cx_number"))
        admin_phone = normalize_phone(latest_row.get("admin_number"))
        direction = normalize_direction(latest_row.get("event_direction") or latest_row.get("direction"))
        customer_dir = "from" if direction != "outbound" else "to"
        admin_dir = "to" if direction != "outbound" else "from"

        if customer_phone:
            participants.append(
                {
                    "participant_seq": 1,
                    "participant_role": "customer",
                    "direction_role": customer_dir,
                    "raw_key_type": "phone",
                    "raw_key_value": customer_phone,
                    "person_id": None,
                }
            )
        if admin_phone:
            participants.append(
                {
                    "participant_seq": 2,
                    "participant_role": "admin_number",
                    "direction_role": admin_dir,
                    "raw_key_type": "phone",
                    "raw_key_value": admin_phone,
                    "person_id": None,
                }
            )
    return participants


def _fallback_topic_label(event_row: dict) -> str:
    family = str(event_row.get("event_family") or "").strip().lower()
    channel = str(event_row.get("event_channel") or "").strip().lower()
    event_name = event_row.get("event_name")
    if family in EVENT_TOPIC_FALLBACK:
        return EVENT_TOPIC_FALLBACK[family]
    if channel in CHANNEL_TOPIC_FALLBACK:
        return CHANNEL_TOPIC_FALLBACK[channel]
    return humanize_label(event_name or family or channel or "general")


def build_customer_overall_summary(db: Session, schema: str, person_ids: List[int]) -> Optional[dict]:
    if not person_ids:
        return None

    in_sql, in_params = build_named_in_params(person_ids, "pid")
    rows = q(
        db,
        f'''
        SELECT DISTINCT
            ef.event_id,
            ef.event_time,
            ef.event_name,
            ef.event_family,
            ef.event_channel,
            ef.event_direction,
            ef.event_status,
            ef.event_meta
        FROM "{schema}".event_fact ef
        JOIN "{schema}".event_participant ep
          ON ep.event_id = ef.event_id
        WHERE ep.person_id IN {in_sql}
        ORDER BY ef.event_time DESC NULLS LAST, ef.event_id DESC
        LIMIT {int(DEFAULT_OVERALL_EVENT_LIMIT)}
        ''',
        in_params,
    )

    if not rows:
        return {
            "person_ids": person_ids,
            "event_count": 0,
            "channels": {},
            "families": {},
            "topic_blend": {},
            "dominant_topics": [],
            "latest_event": None,
            "latest_by_channel": {},
            "recent_events": [],
        }

    channels = Counter()
    families = Counter()
    topics = Counter()
    latest_by_channel: Dict[str, Dict[str, Any]] = {}
    recent_events: List[Dict[str, Any]] = []

    for row in rows:
        channel = str(row.get("event_channel") or "unknown")
        family = str(row.get("event_family") or "unknown")
        channels[channel] += 1
        families[family] += 1

        meta = parse_jsonish(row.get("event_meta"))
        ontology = meta.get("message_ontology") if isinstance(meta.get("message_ontology"), dict) else {}
        if channel == "whatsapp":
            ontology = _apply_property_share_guard(
                ontology,
                clean_content=meta.get("content_preview"),
                message_type=meta.get("message_type"),
                direction=row.get("event_direction") or meta.get("direction"),
            )

        topic_label = humanize_label(ontology.get("topic_primary")) if ontology.get("topic_primary") else _fallback_topic_label(row)
        topics[topic_label] += 1

        if channel not in latest_by_channel:
            latest_by_channel[channel] = {
                "event_time": row.get("event_time"),
                "event_name": row.get("event_name"),
                "event_family": row.get("event_family"),
                "direction": row.get("event_direction"),
                "topic": topic_label,
                "preview": meta.get("content_preview"),
            }

        if len(recent_events) < 12:
            recent_events.append(
                {
                    "event_time": row.get("event_time"),
                    "event_name": row.get("event_name"),
                    "event_family": row.get("event_family"),
                    "event_channel": row.get("event_channel"),
                    "event_direction": row.get("event_direction"),
                    "topic": topic_label,
                    "preview": meta.get("content_preview"),
                }
            )

    latest_row = rows[0]
    latest_meta = parse_jsonish(latest_row.get("event_meta"))
    latest_ontology = latest_meta.get("message_ontology") if isinstance(latest_meta.get("message_ontology"), dict) else {}
    if str(latest_row.get("event_channel") or "").strip().lower() == "whatsapp":
        latest_ontology = _apply_property_share_guard(
            latest_ontology,
            clean_content=latest_meta.get("content_preview"),
            message_type=latest_meta.get("message_type"),
            direction=latest_row.get("event_direction") or latest_meta.get("direction"),
        )
    latest_topic = humanize_label(latest_ontology.get("topic_primary")) if latest_ontology.get("topic_primary") else _fallback_topic_label(latest_row)

    return {
        "person_ids": person_ids,
        "event_count": len(rows),
        "channels": dict(channels),
        "families": dict(families),
        "topic_blend": dict(topics.most_common(12)),
        "dominant_topics": [topic for topic, _count in topics.most_common(5)],
        "latest_event": {
            "event_time": latest_row.get("event_time"),
            "event_name": latest_row.get("event_name"),
            "event_family": latest_row.get("event_family"),
            "event_channel": latest_row.get("event_channel"),
            "event_direction": latest_row.get("event_direction"),
            "topic": latest_topic,
            "preview": latest_meta.get("content_preview"),
        },
        "latest_by_channel": latest_by_channel,
        "recent_events": recent_events,
    }


def conversation_payload(args: argparse.Namespace) -> dict:
    with get_session() as db:
        anchor = resolve_anchor(db, args.schema, args)
        threads = fetch_threads(db, args.schema, anchor, args) if args.list_threads or has_explicit_thread_selector(args) else []
        customer_overall_summary = build_customer_overall_summary(
            db,
            args.schema,
            anchor.get("person_ids") or [],
        ) if args.include_overall_summary else None

        list_only = bool(args.list_threads and not has_explicit_thread_selector(args))

        if list_only:
            messages: List[Dict[str, Any]] = []
            turns: List[Dict[str, Any]] = []
            summary: Dict[str, Any] = {}
            latest_message = None
            participants: List[Dict[str, Any]] = []
        else:
            messages = fetch_messages(db, args.schema, anchor, args)
            turns = build_whatsapp_turns(messages)
            summary = build_whatsapp_conversation_summary(turns)
            latest_message = messages[-1] if messages else None
            participants = fetch_latest_participants(
                db,
                args.schema,
                latest_event_id=latest_message.get("event_id") if latest_message else None,
                latest_row=latest_message,
            )

    latest_ontology = (latest_message or {}).get("message_ontology") or {}
    return {
        "search_input": {
            "phone": args.phone,
            "actor": args.actor,
            "lead_id": args.lead_id,
            "thread_key": args.thread_key,
            "remote_jid": args.remote_jid,
            "admin_number": args.admin_number,
            "customer_number": args.customer_number,
        },
        "resolved": anchor,
        "threads": threads,
        "list_only": list_only,
        "customer_overall_summary": customer_overall_summary,
        "latest_message": latest_message,
        "latest_message_tags": latest_ontology,
        "conversation_summary": summary,
        "turns": turns,
        "messages": messages,
        "participants": participants,
    }


def print_section(title: str) -> None:
    print(title)
    print("-" * 100)


def render_thread_list(threads: List[Dict[str, Any]]) -> None:
    print_section("THREADS")
    if not threads:
        print("  (no WhatsApp threads found)")
        print()
        return

    for thread in threads:
        print(
            f"  - thread_key={fmt_value(thread.get('thread_key'))} | "
            f"kind={fmt_value(thread.get('conversation_kind'))} | "
            f"messages={fmt_value(thread.get('message_count'))} | "
            f"last_time={fmt_dt(thread.get('last_message_time'))}"
        )
        print(
            f"    admin={fmt_value(thread.get('admin_number'))} | "
            f"customer={fmt_value(thread.get('customer_number'))} | "
            f"remote_jid={fmt_value(thread.get('remote_jid'))}"
        )
        print(
            f"    latest_topic={fmt_value(thread.get('latest_topic'))} | "
            f"latest_intent={fmt_value(thread.get('latest_intent'))} | "
            f"dirs=inbound:{fmt_value(thread.get('inbound_messages'))}, outbound:{fmt_value(thread.get('outbound_messages'))}"
        )
        print(f"    preview={fmt_value(thread.get('latest_preview'))}")
    print()


def render_customer_overall_summary(summary: Optional[dict]) -> None:
    if not summary:
        return

    print_section("CUSTOMER OVERALL SUMMARY")
    print(f"  person_ids       : {summary.get('person_ids') or []}")
    print(f"  event_count      : {fmt_value(summary.get('event_count'))}")
    print(f"  channels         : {summary.get('channels') or {}}")
    print(f"  families         : {summary.get('families') or {}}")
    print(f"  topic_blend      : {summary.get('topic_blend') or {}}")
    dominant_topics = summary.get("dominant_topics") or []
    print(f"  dominant_topics  : {', '.join(dominant_topics) if dominant_topics else '-'}")

    latest_event = summary.get("latest_event") or {}
    if latest_event:
        print("  latest_event     :")
        print(
            f"    - {fmt_dt(latest_event.get('event_time'))} | "
            f"channel={fmt_value(latest_event.get('event_channel'))} | "
            f"family={fmt_value(latest_event.get('event_family'))} | "
            f"topic={fmt_value(latest_event.get('topic'))} | "
            f"preview={fmt_value(latest_event.get('preview'))}"
        )

    latest_by_channel = summary.get("latest_by_channel") or {}
    if latest_by_channel:
        print("  latest_by_channel:")
        for channel, row in latest_by_channel.items():
            print(
                f"    - {channel} | {fmt_dt(row.get('event_time'))} | "
                f"topic={fmt_value(row.get('topic'))} | preview={fmt_value(row.get('preview'))}"
            )
    print()


def render_text(payload: dict) -> None:
    """Compact default terminal output for business review. Full detail remains available with --json."""
    search_input = payload.get("search_input") or {}
    resolved = payload.get("resolved") or {}
    threads = payload.get("threads") or []
    list_only = bool(payload.get("list_only"))
    customer_overall_summary = payload.get("customer_overall_summary")
    latest_message = payload.get("latest_message") or {}
    latest_tags = payload.get("latest_message_tags") or {}
    summary = payload.get("conversation_summary") or {}
    turns = payload.get("turns") or []
    messages = payload.get("messages") or []
    participants = payload.get("participants") or []

    def clean_inline(value, max_len: int = 180) -> str:
        if value in (None, "", [], {}):
            return "-"
        text_value = re.sub(r"\s+", " ", str(value)).strip()
        if len(text_value) > max_len:
            return text_value[: max_len - 3].rstrip() + "..."
        return text_value

    def compact_kv(items) -> str:
        parts = []
        for key, value in items:
            if value in (None, "", [], {}):
                continue
            parts.append(f"{key}={clean_inline(value, 120)}")
        return " | ".join(parts) if parts else "-"

    def direction_mix(turn: dict) -> str:
        counts = turn.get("direction_counts") or {}
        outgoing = int(counts.get("outgoing") or counts.get("outbound") or 0)
        incoming = int(counts.get("incoming") or counts.get("inbound") or 0)
        parts = []
        if outgoing:
            parts.append(f"out={outgoing}")
        if incoming:
            parts.append(f"in={incoming}")
        for k, v in counts.items():
            if k not in {"outgoing", "outbound", "incoming", "inbound"} and v:
                parts.append(f"{k}={v}")
        return " ".join(parts) if parts else "-"

    selector = compact_kv([
        ("phone", search_input.get("phone")),
        ("actor", search_input.get("actor")),
        ("lead_id", search_input.get("lead_id")),
        ("thread", search_input.get("thread_key")),
        ("jid", search_input.get("remote_jid")),
        ("admin", search_input.get("admin_number")),
        ("customer", search_input.get("customer_number")),
    ])

    print("WA_CONVERSATION")
    print(f"input: {selector}")
    print(
        "resolved: "
        + compact_kv([
            ("person_ids", resolved.get("person_ids") or None),
            ("lead_id", resolved.get("lead_id")),
            ("admin", resolved.get("admin_number")),
            ("customer", resolved.get("customer_number")),
            ("exec", resolved.get("executive_id")),
            ("kind", resolved.get("conversation_kind")),
            ("thread", resolved.get("thread_key") or resolved.get("conversation_key")),
        ])
    )

    if threads:
        print(f"threads: {len(threads)}")
        for row in threads[:12]:
            print(
                "- "
                f"last={fmt_dt(row.get('last_message_time'))} | "
                f"msgs={fmt_value(row.get('message_count'))} | "
                f"admin={fmt_value(row.get('admin_number'))} | "
                f"customer={fmt_value(row.get('customer_number'))} | "
                f"kind={fmt_value(row.get('conversation_kind'))} | "
                f"thread={fmt_value(row.get('thread_key'))}"
            )
        if len(threads) > 12:
            print(f"... {len(threads) - 12} more threads")

    if customer_overall_summary:
        current_state = customer_overall_summary.get("current_state") or {}
        print(
            "overall: "
            + compact_kv([
                ("stage", current_state.get("journey_stage")),
                ("health", current_state.get("relationship_health")),
                ("next", current_state.get("next_best_action")),
            ])
        )

    if list_only:
        return

    print(f"counts: messages={len(messages)} | turns={len(turns)} | participants={len(participants)}")
    print(
        "summary: "
        + compact_kv([
            ("state", summary.get("current_state")),
            ("topic", summary.get("latest_topic")),
            ("intent", summary.get("latest_intent")),
            ("next", summary.get("next_best_action")),
            ("open_asks", len(summary.get("open_asks") or [])),
        ])
    )

    if latest_message:
        tags = latest_tags or latest_message.get("message_ontology") or {}
        print(
            "latest: "
            f"{fmt_dt(latest_message.get('message_time'))} | "
            f"{fmt_value(latest_message.get('event_direction') or latest_message.get('direction'))} | "
            f"topic={humanize_label(tags.get('topic_primary'))} | "
            f"intent={humanize_label(tags.get('intent_primary'))} | "
            f"{clean_inline(latest_message.get('preview'), 220)}"
        )
    else:
        print("latest: no messages found")

    if not turns and not messages:
        print("timeline: no messages found")
        return

    if turns:
        print("turns:")
        for turn in turns[-10:]:
            print(
                f"{turn.get('turn_id')}. "
                f"{fmt_dt(turn.get('start_time'))} -> {fmt_dt(turn.get('end_time'))} | "
                f"{humanize_label(turn.get('topic_primary'))} | "
                f"{humanize_label(turn.get('intent_primary'))} | "
                f"{direction_mix(turn)} | "
                f"{clean_inline(turn.get('summary'), 240)}"
            )
        return

    print("timeline:")
    for row in messages[-20:]:
        ontology = row.get("message_ontology") or {}
        print(
            f"{fmt_dt(row.get('message_time'))} | "
            f"{fmt_value(row.get('event_direction') or row.get('direction'))} | "
            f"{humanize_label(ontology.get('topic_primary'))} | "
            f"{clean_inline(row.get('preview'), 220)}"
        )

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect one WhatsApp thread with message-level ontology, thread listing, and customer summary")
    parser.add_argument("--phone", help="Customer/admin phone number")
    parser.add_argument("--actor", help="Executive id / actor ref")
    parser.add_argument("--lead-id", type=int, dest="lead_id")
    parser.add_argument("--thread-key", help="Exact WhatsApp thread key like jid:<group_jid> or wa:<admin_phone>-><customer_phone>")
    parser.add_argument("--remote-jid", help="Exact WhatsApp remote_jid for group/direct thread")
    parser.add_argument("--admin-number", help="Business/admin WhatsApp number")
    parser.add_argument("--customer-number", help="Customer WhatsApp number")
    parser.add_argument("--list-threads", action="store_true", help="List all WhatsApp threads for the resolved user/input")
    parser.add_argument("--include-overall-summary", action="store_true", help="Include person-wise overall summary across all event channels")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        payload = conversation_payload(args)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2

    if args.json:
        print(json.dumps(payload, default=str, indent=2, ensure_ascii=False))
        return 0

    render_text(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
