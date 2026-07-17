#!/usr/bin/env python3
"""
review_number_communication.py

Lean communication review for one phone.

Purpose:
  - Review all calls and WhatsApp messages where this phone participated.
  - Works for admin numbers or customer/counterparty numbers.
  - Does NOT resolve booking_id, lead_id, user_id, person_id, or email.
  - Does NOT expand to related contacts. This prevents unrelated/ambiguous rows.

Examples:
  python review_number_communication.py --phone 919480402199 --days 30
  python review_number_communication.py --phone 919480402199 --role admin --days 30 --format json
  python review_number_communication.py --phone 7975826393 --role counterparty --from-date 2026-04-01 --to-date 2026-04-30
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


DEFAULT_SCHEMA = os.getenv("SCHEMA_NAME", "AnalyticsEngine")
IST_OFFSET = timedelta(hours=5, minutes=30)
TIMEZONE_NAME = "Asia/Kolkata"
NON_DIGIT_RE = re.compile(r"\D+")

BUSINESS_TO_COUNTERPARTY = {"outgoing", "outbound", "sent", "reply", "from_admin", "dialed", "dial", "out"}
COUNTERPARTY_TO_BUSINESS = {"incoming", "inbound", "received", "from_customer", "missed", "receive", "in"}
MISSED_STATUSES = {"missed", "not connected", "not_connected", "busy", "no answer", "no_answer", "rejected"}

CONTEXT_ONLY_TYPES = {
    "messagecontextinfo", "contextinfo", "protocolmessage", "senderkeydistributionmessage",
    "ephemeralmessage", "keepinchatmessage",
}
MEDIA_TYPES = {
    "imagemessage": "image", "image": "image",
    "videomessage": "video", "video": "video",
    "audiomessage": "audio", "audio": "audio", "ptt": "audio", "voicemessage": "audio",
    "documentmessage": "document", "document": "document",
    "locationmessage": "location", "livelocationmessage": "location", "location": "location",
    "stickermessage": "sticker", "sticker": "sticker",
    "contactmessage": "contact", "contactsarraymessage": "contact", "contact": "contact",
}
ACK_TEXT = {"ok", "okay", "k", "kk", "done", "noted", "thanks", "thank you", "yes", "ya", "yep", "👍", "👍👍"}

AUTOMATION_PATTERNS = [
    ("auto_payment_ack", re.compile(r"\b(thank you for updating your .*reference|we(?:'|’)?ve received your payment|we have received your payment|received your payment of|netbanking/upi reference)\b", re.I)),
    ("auto_welcome", re.compile(r"\b(welcome to rentmystay|thank you for choosing rentmystay|welcome to your new home|download our app)\b", re.I)),
    ("auto_agreement", re.compile(r"\b(rental agreement .*renewed|agreement .*renewed|complete your kyc|sign the agreement|smooth check-in)\b", re.I)),
    ("auto_ticket_update", re.compile(r"\b(your issue with ticket number|issue with ticket number .*resolved|representative has visited your flat for ticket)\b", re.I)),
    ("auto_booking", re.compile(r"\b(thanks for booking with rentmystay|booking id|bkid|this is a reminder that your check-in|check-in is at|check-out date)\b", re.I)),
]

KEYWORDS = {
    "payment": ("payment", "paid", "rent", "cash", "upi", "refund", "deposit", "amount", "balance", "receipt", "reference", "utr", "invoice"),
    "support": ("issue", "problem", "not working", "fan", "power", "electric", "water", "leak", "damage", "broken", "repair", "maintenance", "complaint", "ticket"),
    "booking": ("extension", "agreement", "kyc", "check in", "check-in", "checkout", "check out", "booking", "cancel", "cancellation", "app", "email", "visit", "occupant"),
    "urgency": ("urgent", "asap", "immediately", "today", "right now", "now", "tomorrow"),
}


def _project_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent, *current.parents]:
        if (parent / "app").is_dir():
            return parent
    return Path.cwd()


PROJECT_ROOT = _project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _try_load_env() -> None:
    if load_dotenv is None:
        return
    for env_path in (PROJECT_ROOT / ".env", PROJECT_ROOT.parent / ".env", Path.cwd() / ".env", Path.cwd().parent / ".env"):
        if env_path.exists():
            load_dotenv(env_path, override=False)


@contextmanager
def get_session(database_url: Optional[str] = None) -> Iterable[Session]:
    repo_gen = None
    repo_db = None
    if not database_url:
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
            repo_db = None
            repo_gen = None

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
    db_url = database_url or os.getenv("DATABASE_URL") or os.getenv("PG_URL")
    if not db_url:
        raise RuntimeError("No DB available. Run inside repo or set DATABASE_URL/PG_URL, or pass --database-url.")
    engine = create_engine(db_url, pool_pre_ping=True)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield db
    finally:
        try:
            db.close()
        finally:
            engine.dispose()


def q(db: Session, sql: str, params: Optional[dict] = None) -> List[Dict[str, Any]]:
    return [dict(row._mapping) for row in db.execute(text(sql), params or {}).fetchall()]


def q1(db: Session, sql: str, params: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    rows = q(db, sql, params)
    return rows[0] if rows else None


def schema_ident(schema: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema or ""):
        raise ValueError(f"Unsafe schema name: {schema!r}")
    return f'"{schema}"'


def table_exists(db: Session, schema: str, table_name: str) -> bool:
    row = q1(
        db,
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = :schema_name AND table_name = :table_name
        ) AS present
        """,
        {"schema_name": schema, "table_name": table_name},
    )
    return bool(row and row.get("present"))


def table_columns(db: Session, schema: str, table_name: str) -> set[str]:
    rows = q(
        db,
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = :schema_name AND table_name = :table_name
        """,
        {"schema_name": schema, "table_name": table_name},
    )
    return {str(row.get("column_name")) for row in rows if row.get("column_name")}


def first_existing(columns: set[str], *names: str) -> Optional[str]:
    for name in names:
        if name in columns:
            return name
    return None


def now_ist_naive() -> datetime:
    return datetime.utcnow() + IST_OFFSET


def parse_date(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


def resolve_window(days: Optional[int], from_date: Optional[date], to_date: Optional[date]) -> Tuple[datetime, datetime, str]:
    if from_date or to_date:
        from_date = from_date or to_date
        to_date = to_date or from_date
        if from_date is None or to_date is None:
            raise ValueError("Invalid date range")
        if from_date > to_date:
            raise ValueError("--from-date cannot be after --to-date")
        start_dt = datetime.combine(from_date, datetime.min.time())
        end_dt = datetime.combine(to_date + timedelta(days=1), datetime.min.time())
        return start_dt, end_dt, f"{from_date.isoformat()} → {to_date.isoformat()}"
    safe_days = 30 if days is None or int(days) <= 0 else int(days)
    end_dt = now_ist_naive()
    start_dt = end_dt - timedelta(days=safe_days)
    return start_dt, end_dt, f"last {safe_days} day(s)"


def norm_digits(value: Any) -> str:
    return NON_DIGIT_RE.sub("", str(value or ""))


def phone_last10(value: Any) -> Optional[str]:
    digits = norm_digits(value)
    if len(digits) == 10:
        return digits
    if len(digits) == 12:
        return digits[-10:]
    return None


def show_phone(value: Any) -> str:
    p10 = phone_last10(value)
    return f"91{p10}" if p10 else ""


def db_phone_expr(column_name: str) -> str:
    return f"RIGHT(REGEXP_REPLACE(COALESCE({column_name}::text, ''), '\\D', '', 'g'), 10)"


def clean_text(value: Any, max_len: int = 220) -> str:
    if value in (None, ""):
        return ""
    out = re.sub(r"\s+", " ", str(value)).strip()
    return out if len(out) <= max_len else out[: max_len - 3].rstrip() + "..."


def fmt_dt(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)[:19]


def fmt_duration(seconds: Any) -> str:
    try:
        s = int(seconds or 0)
    except Exception:
        return str(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def normalize_direction(value: Any) -> str:
    return str(value or "").strip().lower()


def business_flow(direction: Any) -> str:
    d = normalize_direction(direction)
    if d in BUSINESS_TO_COUNTERPARTY:
        return "business_to_counterparty"
    if d in COUNTERPARTY_TO_BUSINESS:
        return "counterparty_to_business"
    return "unknown"


def normalize_message_type(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def message_kind(message_type: Any, message_text: Any) -> str:
    mt = normalize_message_type(message_type)
    txt = clean_text(message_text, 1000)
    if mt in CONTEXT_ONLY_TYPES and not txt:
        return "context"
    if mt in MEDIA_TYPES and not txt:
        return MEDIA_TYPES[mt]
    if txt:
        return "ack" if txt.strip().lower() in ACK_TEXT else "text"
    return mt or "empty"


def call_transcript_text(row: Dict[str, Any], max_len: int) -> str:
    return clean_text(
        row.get("translated_text")
        or row.get("transcript_text")
        or row.get("transcript_text_eleven_labs")
        or row.get("raw_transcripts"),
        max_len,
    )


def classify_text_category(text_value: str, flow: str, kind: str) -> str:
    text_l = (text_value or "").lower()
    for label, pattern in AUTOMATION_PATTERNS:
        if flow == "business_to_counterparty" and pattern.search(text_l):
            return label
    if kind in MEDIA_TYPES.values():
        return f"media_{kind}"
    for category, terms in KEYWORDS.items():
        if any(term in text_l for term in terms):
            return f"manual_{category}"
    if kind == "ack":
        return "ack"
    return "manual_text" if text_value else "empty"


def priority_for_event(channel: str, flow: str, category: str, kind: str) -> str:
    if category.startswith("auto_"):
        return "P4"
    if channel == "call":
        return "P1" if flow == "counterparty_to_business" else "P2"
    if flow == "counterparty_to_business" and category in {"manual_payment", "manual_support", "manual_booking", "manual_urgency"}:
        return "P1"
    if flow == "counterparty_to_business" and kind not in {"ack", "empty", "context"}:
        return "P2"
    if category.startswith("manual_") or category.startswith("media_"):
        return "P2"
    return "P4"


def role_condition(phone_p10: str, role: str, *, admin_col: str, counterparty_col: str) -> Tuple[str, Dict[str, Any]]:
    params = {"phone10": phone_p10}
    if role == "admin":
        return f"{db_phone_expr(admin_col)} = :phone10", params
    if role == "counterparty":
        return f"{db_phone_expr(counterparty_col)} = :phone10", params
    return f"({db_phone_expr(admin_col)} = :phone10 OR {db_phone_expr(counterparty_col)} = :phone10)", params


def _sql_col_or_null(column: Optional[str], alias: str) -> str:
    return f"{column} AS {alias}" if column else f"NULL AS {alias}"


def _parse_duration_seconds(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text_value = str(value).strip()
    if not text_value:
        return 0
    if text_value.isdigit():
        return int(text_value)
    parts = text_value.split(":")
    if len(parts) in (2, 3) and all(part.strip().isdigit() for part in parts):
        nums = [int(part) for part in parts]
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    match = re.search(r"\d+", text_value)
    return int(match.group(0)) if match else 0


def fetch_calls(db: Session, schema: str, phone_p10: str, role: str, start_dt: datetime, end_dt: datetime, limit: int, max_text: int) -> List[Dict[str, Any]]:
    table_name = "staging_call_recordings_transcript"
    if not table_exists(db, schema, table_name):
        return []

    columns = table_columns(db, schema, table_name)
    time_col = first_existing(columns, "call_time", "call_date", "created_at", "uploaded_at")
    admin_col = first_existing(columns, "sales_phone", "sales_phone_number", "admin_number", "business_phone")
    counterparty_col = first_existing(columns, "counterparty_phone", "ph_num", "customer_phone", "phone", "mobile")
    duration_col = first_existing(columns, "talk_time_sec", "call_duration", "duration_sec", "duration")
    direction_col = first_existing(columns, "call_direction", "call_type", "direction")
    result_col = first_existing(columns, "call_result", "call_status", "status", "source_status")
    source_id_col = first_existing(columns, "source_id", "id")

    # Required minimum: a time column and at least one phone-side column.
    # Supports the known current and legacy call-log column shapes without broad identity expansion.
    if not time_col or not (admin_col or counterparty_col):
        return []

    match_terms: List[str] = []
    match_params: Dict[str, Any] = {"phone10": phone_p10}
    if role == "admin":
        if admin_col:
            match_terms.append(f"{db_phone_expr(admin_col)} = :phone10")
    elif role == "counterparty":
        if counterparty_col:
            match_terms.append(f"{db_phone_expr(counterparty_col)} = :phone10")
    else:
        if admin_col:
            match_terms.append(f"{db_phone_expr(admin_col)} = :phone10")
        if counterparty_col:
            match_terms.append(f"{db_phone_expr(counterparty_col)} = :phone10")
    if not match_terms:
        return []

    sch = schema_ident(schema)
    select_parts = [
        _sql_col_or_null(source_id_col, "source_id"),
        _sql_col_or_null("lead_id" if "lead_id" in columns else None, "lead_id"),
        _sql_col_or_null("executive_id" if "executive_id" in columns else None, "executive_id"),
        _sql_col_or_null("executive_name" if "executive_name" in columns else None, "executive_name"),
        f"{time_col} AS event_time",
        _sql_col_or_null(direction_col, "call_direction"),
        _sql_col_or_null(result_col, "call_result"),
        _sql_col_or_null(duration_col, "talk_time_sec"),
        _sql_col_or_null(counterparty_col, "counterparty_phone"),
        _sql_col_or_null(admin_col, "sales_phone"),
        _sql_col_or_null("audio_url" if "audio_url" in columns else None, "audio_url"),
        _sql_col_or_null("translated_text" if "translated_text" in columns else None, "translated_text"),
        _sql_col_or_null("transcript_text" if "transcript_text" in columns else None, "transcript_text"),
        _sql_col_or_null("transcript_text_eleven_labs" if "transcript_text_eleven_labs" in columns else None, "transcript_text_eleven_labs"),
        _sql_col_or_null("raw_transcripts" if "raw_transcripts" in columns else None, "raw_transcripts"),
        _sql_col_or_null("intent" if "intent" in columns else None, "intent"),
        _sql_col_or_null("emotion" if "emotion" in columns else None, "emotion"),
        _sql_col_or_null("tone" if "tone" in columns else None, "tone"),
        _sql_col_or_null("action_layer" if "action_layer" in columns else None, "action_layer"),
        _sql_col_or_null("context" if "context" in columns else None, "context"),
        _sql_col_or_null("outcome" if "outcome" in columns else None, "outcome"),
        _sql_col_or_null("language" if "language" in columns else None, "language"),
    ]
    order_tail = f", {source_id_col} ASC" if source_id_col else ""
    rows = q(
        db,
        f"""
        SELECT {', '.join(select_parts)}
        FROM {sch}.{table_name}
        WHERE {time_col} >= :start_dt AND {time_col} < :end_dt
          AND ({' OR '.join(match_terms)})
        ORDER BY {time_col} ASC NULLS LAST{order_tail}
        LIMIT :limit_n
        """,
        {**match_params, "start_dt": start_dt, "end_dt": end_dt, "limit_n": int(limit)},
    )
    out: List[Dict[str, Any]] = []
    for row in rows:
        direction = normalize_direction(row.get("call_direction"))
        flow = business_flow(direction)
        duration = _parse_duration_seconds(row.get("talk_time_sec"))
        status = normalize_direction(row.get("call_result")) or ("connected" if duration > 0 else "missed")
        transcript = call_transcript_text(row, max_text)
        kind = "call_connected" if duration > 0 and status not in MISSED_STATUSES else "call_missed"
        category = "manual_call"
        priority = priority_for_event("call", flow, category, kind)
        summary = f"Call {status or '-'} duration={fmt_duration(duration)}"
        out.append({
            "channel": "call",
            "source_id": row.get("source_id") or "",
            "lead_id": row.get("lead_id") or "",
            "executive_id": row.get("executive_id") or "",
            "executive_name": row.get("executive_name") or "",
            "event_time": row.get("event_time"),
            "direction": direction,
            "business_flow": flow,
            "status": status,
            "admin_number": show_phone(row.get("sales_phone")),
            "counterparty_number": show_phone(row.get("counterparty_phone")),
            "sender": show_phone(row.get("sales_phone")) if flow == "business_to_counterparty" else show_phone(row.get("counterparty_phone")),
            "receiver": show_phone(row.get("counterparty_phone")) if flow == "business_to_counterparty" else show_phone(row.get("sales_phone")),
            "kind": kind,
            "category": category,
            "priority": priority,
            "needs_review": priority in {"P1", "P2"},
            "duration_sec": duration,
            "text": f"{summary} | Transcript: {transcript}" if transcript else summary,
            "transcript": transcript,
            "audio_url": row.get("audio_url") or "",
            "intent": row.get("intent") or "",
            "emotion": row.get("emotion") or "",
            "tone": row.get("tone") or "",
            "outcome": row.get("outcome") or "",
        })
    return out

def fetch_whatsapp(db: Session, schema: str, phone_p10: str, role: str, start_dt: datetime, end_dt: datetime, limit: int, max_text: int) -> List[Dict[str, Any]]:
    if not table_exists(db, schema, "staging_whatsapp_messages"):
        return []
    sch = schema_ident(schema)
    cond, params = role_condition(phone_p10, role, admin_col="admin_number", counterparty_col="cx_number")
    rows = q(
        db,
        f"""
        SELECT source_id::text, lead_id::text, executive_id,
               message_time AS event_time, direction,
               CASE
                    WHEN COALESCE(isread::text, '') IN ('1','true','t','yes') THEN 'read'
                    WHEN COALESCE(issent::text, '') IN ('1','true','t','yes') THEN 'sent'
                    ELSE NULL
               END AS status,
               admin_number, cx_number AS counterparty_number,
               message_type, clean_content AS message_text, remote_jid
        FROM {sch}.staging_whatsapp_messages
        WHERE message_time >= :start_dt AND message_time < :end_dt
          AND ({cond})
        ORDER BY message_time ASC NULLS LAST, source_id ASC
        LIMIT :limit_n
        """,
        {**params, "start_dt": start_dt, "end_dt": end_dt, "limit_n": int(limit)},
    )
    out: List[Dict[str, Any]] = []
    for row in rows:
        kind = message_kind(row.get("message_type"), row.get("message_text"))
        if kind == "context":
            continue
        text_value = clean_text(row.get("message_text"), max_text)
        if not text_value and kind in MEDIA_TYPES.values():
            text_value = f"[{kind}]"
        flow = business_flow(row.get("direction"))
        category = classify_text_category(text_value, flow, kind)
        priority = priority_for_event("whatsapp", flow, category, kind)
        admin = show_phone(row.get("admin_number"))
        counterparty = show_phone(row.get("counterparty_number"))
        out.append({
            "channel": "whatsapp",
            "source_id": row.get("source_id"),
            "lead_id": row.get("lead_id") or "",
            "executive_id": row.get("executive_id") or "",
            "event_time": row.get("event_time"),
            "direction": normalize_direction(row.get("direction")),
            "business_flow": flow,
            "status": normalize_direction(row.get("status")),
            "admin_number": admin,
            "counterparty_number": counterparty,
            "sender": admin if flow == "business_to_counterparty" else counterparty,
            "receiver": counterparty if flow == "business_to_counterparty" else admin,
            "kind": kind,
            "category": category,
            "priority": priority,
            "needs_review": priority in {"P1", "P2"},
            "text": text_value,
            "remote_jid": row.get("remote_jid") or "",
        })
    return out


def keep_row(row: Dict[str, Any], *, focus: str, hide_automation: bool) -> bool:
    category = str(row.get("category") or "")
    if hide_automation and category.startswith("auto_"):
        return False
    if focus == "all":
        return True
    if focus == "review":
        return bool(row.get("needs_review"))
    if focus == "manual":
        return not category.startswith("auto_")
    if focus == "automation":
        return category.startswith("auto_")
    return True


def build_payload(args: argparse.Namespace) -> Dict[str, Any]:
    phone_p10 = phone_last10(args.phone)
    if not phone_p10:
        raise ValueError("Provide a valid --phone with at least 10 digits.")
    start_dt, end_dt, window_label = resolve_window(args.days, parse_date(args.from_date), parse_date(args.to_date))
    with get_session(args.database_url) as db:
        rows = []
        rows.extend(fetch_calls(db, args.schema, phone_p10, args.role, start_dt, end_dt, args.limit, args.max_text))
        rows.extend(fetch_whatsapp(db, args.schema, phone_p10, args.role, start_dt, end_dt, args.limit, args.max_text))
    rows = [r for r in rows if keep_row(r, focus=args.focus, hide_automation=args.hide_automation)]
    rows.sort(key=lambda r: (fmt_dt(r.get("event_time")), str(r.get("channel")), str(r.get("source_id"))))

    counts = {
        "events": len(rows),
        "calls": sum(1 for r in rows if r.get("channel") == "call"),
        "whatsapp": sum(1 for r in rows if r.get("channel") == "whatsapp"),
        "business_to_counterparty": sum(1 for r in rows if r.get("business_flow") == "business_to_counterparty"),
        "counterparty_to_business": sum(1 for r in rows if r.get("business_flow") == "counterparty_to_business"),
        "needs_review": sum(1 for r in rows if r.get("needs_review")),
    }
    return {
        "context_version": "review_number_communication:v2_phone_only",
        "input": {
            "phone": show_phone(phone_p10),
            "role": args.role,
            "window": window_label,
            "from": start_dt.isoformat(sep=" "),
            "to": end_dt.isoformat(sep=" "),
            "schema": args.schema,
            "timezone": TIMEZONE_NAME,
        },
        "counts": counts,
        "events": rows,
    }


def render_table(payload: Dict[str, Any], max_rows: int) -> str:
    lines: List[str] = []
    inp = payload["input"]
    counts = payload["counts"]
    rows = payload["events"][:max_rows]
    lines.append("=" * 120)
    lines.append("NUMBER COMMUNICATION REVIEW")
    lines.append("=" * 120)
    lines.append(f"phone={inp['phone']} | role={inp['role']} | window={inp['window']} | events={counts['events']} | calls={counts['calls']} | whatsapp={counts['whatsapp']} | review={counts['needs_review']}")
    lines.append("-" * 120)
    if not rows:
        lines.append("No matching communication found.")
        return "\n".join(lines)
    for row in rows:
        lines.append(
            f"[{fmt_dt(row.get('event_time'))}] {str(row.get('channel')).upper()} {row.get('business_flow')} "
            f"status={row.get('status') or '-'} priority={row.get('priority') or '-'} "
            f"admin={row.get('admin_number') or '-'} counterparty={row.get('counterparty_number') or '-'} "
            f"lead={row.get('lead_id') or '-'} src={row.get('source_id') or '-'}"
        )
        text_value = clean_text(row.get("text"), 500)
        if text_value:
            lines.append(f"  {text_value}")
        lines.append("")
    if len(payload["events"]) > max_rows:
        lines.append(f"... truncated: showing {max_rows} of {len(payload['events'])} events")
    return "\n".join(lines).rstrip()


def write_csv(payload: Dict[str, Any], path: str) -> None:
    fields = [
        "event_time", "channel", "business_flow", "direction", "status", "priority", "needs_review",
        "admin_number", "counterparty_number", "sender", "receiver", "lead_id", "executive_id", "source_id",
        "kind", "category", "text", "audio_url", "remote_jid",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in payload["events"]:
            writer.writerow({**row, "event_time": fmt_dt(row.get("event_time"))})


def main() -> int:
    parser = argparse.ArgumentParser(description="Review communication for one exact phone number only.")
    parser.add_argument("--phone", required=True, help="Phone to review. Accepts 10 digits or 12 digits; matches by last 10 digits.")
    parser.add_argument("--role", choices=["any", "admin", "counterparty"], default="any", help="Where the phone should match.")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--from-date", default=None, help="YYYY-MM-DD")
    parser.add_argument("--to-date", default=None, help="YYYY-MM-DD")
    parser.add_argument("--view", choices=["clean", "raw"], default="clean", help="Reserved for UI compatibility; output remains compact.")
    parser.add_argument("--focus", choices=["all", "review", "manual", "automation"], default="all")
    parser.add_argument("--hide-automation", action="store_true")
    parser.add_argument("--format", choices=["table", "json", "csv"], default="table")
    parser.add_argument("--output", default=None)
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--print-limit", type=int, default=200)
    parser.add_argument("--max-text", type=int, default=220)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()

    payload = build_payload(args)
    if args.format == "json":
        rendered = json.dumps(payload, default=str, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).write_text(rendered, encoding="utf-8")
        else:
            print(rendered)
    elif args.format == "csv":
        if not args.output:
            raise ValueError("--output is required for --format csv")
        write_csv(payload, args.output)
        print(f"Wrote {len(payload['events'])} rows to {args.output}")
    else:
        rendered = render_table(payload, args.print_limit)
        if args.output:
            Path(args.output).write_text(rendered, encoding="utf-8")
        else:
            print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
