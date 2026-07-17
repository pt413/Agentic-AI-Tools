#!/usr/bin/env python3
"""
review_lead_communication.py

Lean communication review for one lead_id.

Purpose:
  - Review sales/lead handling quality for a single lead.
  - Uses lead_id as the only seed.
  - Does NOT accept booking_id/phone/email/user_id/person_id.
  - Avoids broad identity expansion and weak contact bridges.

Evidence rules:
  - Lead row from staging_lead_tracking.
  - WhatsApp rows matched by the lead row customer phone(s).
  - Call rows where staging_call_log_unified.lead_id = lead_id.
  - Email rows only for the email present on the lead row, if any.
  - Site visits where staging_site_visits.lead_id = lead_id.
  - Travel-cart / booking-attempt rows where staging_travel_cart.user_id = lead.user_id.

Examples:
  python review_lead_communication.py --lead-id 401676
  python review_lead_communication.py --lead-id 401676 --llm
  python review_lead_communication.py --lead-id 401676 --format json --output lead_401676.json
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


DEFAULT_SCHEMA = os.getenv("SCHEMA_NAME", "AnalyticsEngine")
IST_OFFSET = timedelta(hours=5, minutes=30)
WORK_START_HOUR = int(os.getenv("LEAD_REVIEW_WORK_START_HOUR", "10"))
WORK_END_HOUR = int(os.getenv("LEAD_REVIEW_WORK_END_HOUR", "20"))
WORK_WINDOW_LABEL = f"{WORK_START_HOUR:02d}:00-{WORK_END_HOUR:02d}:00 Asia/Kolkata"
NON_DIGIT_RE = re.compile(r"\D+")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

COMPANY_TO_CUSTOMER = {"outgoing", "outbound", "sent", "reply", "from_admin", "dialed", "dial", "out"}
CUSTOMER_TO_COMPANY = {"incoming", "inbound", "received", "from_customer", "missed", "receive", "in"}

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

KEYWORDS = {
    "payment": ("payment", "paid", "rent", "cash", "upi", "refund", "deposit", "amount", "balance", "receipt", "reference", "utr", "invoice"),
    "support": ("issue", "problem", "not working", "fan", "power", "electric", "water", "leak", "damage", "broken", "repair", "maintenance", "complaint", "ticket"),
    "booking": ("extension", "agreement", "kyc", "check in", "check-in", "checkout", "check out", "booking", "cancel", "cancellation", "app", "email", "visit", "occupant"),
    "urgency": ("urgent", "asap", "immediately", "today", "right now", "now", "tomorrow"),
}

AUTOMATION_PATTERNS = [
    ("auto_payment_ack", re.compile(r"\b(thank you for updating your .*reference|we(?:'|’)?ve received your payment|we have received your payment|received your payment of|netbanking/upi reference)\b", re.I)),
    ("auto_welcome", re.compile(r"\b(welcome to rentmystay|thank you for choosing rentmystay|welcome to your new home|download our app)\b", re.I)),
    ("auto_agreement", re.compile(r"\b(rental agreement .*renewed|agreement .*renewed|complete your kyc|sign the agreement|smooth check-in)\b", re.I)),
    ("auto_ticket_update", re.compile(r"\b(your issue with ticket number|issue with ticket number .*resolved|representative has visited your flat for ticket)\b", re.I)),
    ("auto_booking", re.compile(r"\b(thanks for booking with rentmystay|booking id|bkid|this is a reminder that your check-in|check-in is at|check-out date)\b", re.I)),
    ("auto_site_visit", re.compile(r"\b(approved site visit|site visit .*confirmed|your site visit is confirmed|hurray! your site visit)\b", re.I)),
]

ROLE_ALIASES = (
    ("caretaker", ("caretaker", "care taker")),
    ("sales", ("sales", "sale", "leasing", "inside sales", "field sales")),
    ("ops", ("ops", "operation", "operations", "property", "supervisor", "superviser", "manager")),
    ("finance", ("finance", "fin", "account", "accounts", "payment")),
    ("support", ("support", "service", "customer care", "helpdesk")),
    ("admin", ("admin", "administrator")),
    ("system", ("system", "automation", "bot", "noreply", "no-reply")),
)

RAW_ONLY_EVENT_FIELDS = {
    "source_id",      # useful for DB debugging, noisy for LLMs; keep only in raw/debug output
    "match_by",       # explains the SQL matching path, not customer-handling evidence
    "direction",      # role-specific flow already carries the useful direction
    "admin_number",   # replaced by actor_role/actor; keep raw-only for troubleshooting
}
LLM_DROP_EVENT_FIELDS = RAW_ONLY_EVENT_FIELDS | {"lead_id", "needs_review"}
EVENT_FIELD_ORDER = (
    "time", "channel", "flow", "status", "actor_role", "actor", "actor_name",
    "customer_number", "customer_email", "sender", "receiver", "kind", "category",
    "priority", "duration_sec", "within_business_hours", "after_hours", "work_window",
    "counts_against_sales_score", "score_impact", "scoring_note", "text", "transcript", "duplicate_count",
    "user_id", "prop_id", "travel_from_date", "travel_to_date", "nights",
    "booking_type", "total_amount", "advance_amount", "pending_amount", "cart_source",
    "source_id", "lead_id", "direction", "admin_number", "needs_review", "match_by",
)


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
    if not table_exists(db, schema, table_name):
        return set()
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


def compact_dict(value: Dict[str, Any]) -> Dict[str, Any]:
    """Drop empty values recursively while preserving 0 and False."""
    out: Dict[str, Any] = {}
    for key, item in (value or {}).items():
        if item in (None, "", [], {}, ()):  # keep 0 / False
            continue
        if isinstance(item, dict):
            cleaned = compact_dict(item)
            if cleaned:
                out[key] = cleaned
        elif isinstance(item, list):
            cleaned_list = []
            for child in item:
                if isinstance(child, dict):
                    cleaned_child = compact_dict(child)
                    if cleaned_child:
                        cleaned_list.append(cleaned_child)
                elif child not in (None, "", [], {}, ()):  # keep 0 / False
                    cleaned_list.append(child)
            if cleaned_list:
                out[key] = cleaned_list
        else:
            out[key] = item
    return out


def order_event_fields(event: Dict[str, Any]) -> Dict[str, Any]:
    ordered: Dict[str, Any] = {}
    for key in EVENT_FIELD_ORDER:
        if key in event:
            ordered[key] = event[key]
    for key in sorted(event.keys()):
        if key not in ordered:
            ordered[key] = event[key]
    return ordered


def normalize_actor_role(value: Any, *, fallback: Optional[str] = None) -> Optional[str]:
    text_value = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if not text_value or text_value in {"0", "none", "null", "na", "n/a", "unassigned"}:
        return fallback
    for canonical, aliases in ROLE_ALIASES:
        if any(alias in text_value for alias in aliases):
            return canonical
    token = re.sub(r"[^a-z0-9]+", "_", text_value).strip("_")
    return token[:40] or fallback


def _email_local_part(value: Any) -> str:
    email = norm_email(value)
    if not email:
        text_value = str(value or "").strip().lower()
        return re.sub(r"[^a-z0-9_.+-]+", "", text_value.split("@", 1)[0])[:40]
    return email.split("@", 1)[0]


def fallback_email_role(email: Any, *, category: Optional[str] = None, default: str = "sales") -> str:
    local = _email_local_part(email)
    if str(category or "").startswith("auto_"):
        return "system"
    if local in {"noreply", "no-reply", "donotreply", "do-not-reply"}:
        return "system"
    if local in {"support", "help", "care"}:
        return "support"
    if local in {"accounts", "account", "finance", "payments", "payment"}:
        return "finance"
    # contact@rentmystay.com is a shared lead channel.  In lead review, treat it
    # as sales unless a staff record says otherwise.
    return default


def is_business_to_customer_flow(flow: Any) -> bool:
    text_value = str(flow or "").strip().lower()
    return text_value.endswith("_to_customer") and not text_value.startswith("customer_to_")


def is_customer_to_business_flow(flow: Any) -> bool:
    return str(flow or "").strip().lower().startswith("customer_to_")


def business_hours_meta(value: Any) -> Dict[str, Any]:
    """Return working-hours metadata for an event timestamp in local IST-naive time."""
    event_dt = parse_event_dt(value)
    if event_dt is None:
        return {"work_window": WORK_WINDOW_LABEL}

    start_min = int(WORK_START_HOUR) * 60
    end_min = int(WORK_END_HOUR) * 60
    event_min = event_dt.hour * 60 + event_dt.minute
    within = start_min <= event_min < end_min
    return {
        "work_window": WORK_WINDOW_LABEL,
        "within_business_hours": within,
        "after_hours": not within,
        "sla_basis": "same_working_window" if within else "next_working_window",
    }


def add_business_hours_scoring(event: Dict[str, Any]) -> Dict[str, Any]:
    """Annotate score-facing vs context-only after-hours customer inbound events."""
    annotated = dict(event or {})
    annotated.update(business_hours_meta(annotated.get("time")))

    is_customer_inbound = is_customer_to_business_flow(annotated.get("flow"))
    is_after_hours = annotated.get("after_hours") is True
    channel = str(annotated.get("channel") or "").strip().lower()
    status = str(annotated.get("status") or "").strip().lower()
    duration = int(annotated.get("duration_sec") or 0)
    is_missed_call = channel == "call" and status == "missed" and duration <= 0

    if is_customer_inbound and is_after_hours:
        annotated["counts_against_sales_score"] = False
        annotated["score_impact"] = "context_only_after_hours"
        annotated["scoring_note"] = "After-hours customer inbound; do not penalize immediately. Judge recovery from next working window."
    elif is_missed_call and is_customer_inbound:
        annotated["counts_against_sales_score"] = True
        annotated["score_impact"] = "score_facing_missed_inbound"
    else:
        annotated["counts_against_sales_score"] = True
        annotated["score_impact"] = "score_facing"

    return annotated


class StaffRoleResolver:
    """Resolve a communication actor into a compact role label for review payloads.

    Resolution order:
      1) staging_user_account by username/email
      2) staging_staff_phone_assignment by pooled/office line phone
      3) staging_user_account by staff phone

    This keeps the output role-specific (sales_to_customer, customer_to_ops, etc.)
    without exposing SQL/debug matching fields to the LLM.
    """

    def __init__(self, db: Session, schema: str):
        self.db = db
        self.schema = schema
        self._columns_cache: Dict[str, set[str]] = {}
        self._cache: Dict[Tuple[str, ...], Dict[str, Any]] = {}

    def _table_ref(self, table_name: str) -> str:
        return f"{schema_ident(self.schema)}.{table_name}"

    def _columns(self, table_name: str) -> set[str]:
        if table_name not in self._columns_cache:
            self._columns_cache[table_name] = table_columns(self.db, self.schema, table_name)
        return self._columns_cache[table_name]

    def _select_existing(self, table_name: str, candidates: List[str]) -> str:
        columns = self._columns(table_name)
        selected = [column for column in candidates if column in columns]
        if not selected:
            return "*"
        return ", ".join(selected)

    @staticmethod
    def _staff_result(row: Optional[Dict[str, Any]], *, source: str, fallback_actor: Any = None, fallback_role: Optional[str] = None) -> Dict[str, Any]:
        if not row:
            actor = clean_text(fallback_actor, 80)
            role = normalize_actor_role(fallback_role)
            return compact_dict({"actor": actor, "actor_role": role})

        actor = (
            clean_text(row.get("username"), 80)
            or clean_text(row.get("tag_to"), 80)
            or clean_text(row.get("tagTo"), 80)
            or clean_text(row.get("email"), 80)
            or clean_text(fallback_actor, 80)
        )
        role = normalize_actor_role(row.get("team"), fallback=normalize_actor_role(fallback_role))
        if not role and row.get("is_admin") not in (None, ""):
            role = "admin" if str(row.get("is_admin")).lower() in {"1", "true", "t", "yes", "admin"} else "staff"
        return compact_dict({"actor": actor, "actor_role": role, "actor_source": source})

    def _lookup_user_account(self, *, username: Any = None, email: Any = None, phone: Any = None) -> Optional[Dict[str, Any]]:
        table_name = "staging_user_account"
        if not table_exists(self.db, self.schema, table_name):
            return None
        columns = self._columns(table_name)
        select_list = self._select_existing(table_name, ["source_id", "username", "email", "phone_number", "normalized_phone", "is_admin", "team", "active"])
        conditions: List[str] = []
        params: Dict[str, Any] = {}

        username_text = clean_text(username, 120)
        email_text = norm_email(email)
        phone10 = phone_last10(phone)
        if username_text and "username" in columns:
            conditions.append("LOWER(TRIM(username::text)) = LOWER(:username)")
            params["username"] = username_text
        if email_text and "email" in columns:
            conditions.append("LOWER(TRIM(email::text)) = :email")
            params["email"] = email_text
        phone_columns = [column for column in ("normalized_phone", "phone_number", "contact_no") if column in columns]
        if phone10 and phone_columns:
            phone_expr = "RIGHT(REGEXP_REPLACE(COALESCE(" + ", ".join(f"{column}::text" for column in phone_columns) + ", ''), '\\D', '', 'g'), 10)"
            conditions.append(f"{phone_expr} = :phone10")
            params["phone10"] = phone10

        if not conditions:
            return None
        order_sql = "ORDER BY active DESC NULLS LAST, source_id DESC NULLS LAST" if "active" in columns and "source_id" in columns else ""
        rows = q(
            self.db,
            f"""
            SELECT {select_list}
            FROM {self._table_ref(table_name)}
            WHERE {' OR '.join(f'({condition})' for condition in conditions)}
            {order_sql}
            LIMIT 1
            """,
            params,
        )
        return rows[0] if rows else None

    def _lookup_phone_assignment(self, phone: Any) -> Optional[Dict[str, Any]]:
        table_name = "staging_staff_phone_assignment"
        phone10 = phone_last10(phone)
        if not phone10 or not table_exists(self.db, self.schema, table_name):
            return None
        columns = self._columns(table_name)
        phone_columns = [column for column in ("normalized_phone", "phone", "phone_number") if column in columns]
        if not phone_columns:
            return None
        select_list = self._select_existing(table_name, ["source_id", "phone", "normalized_phone", "tag_to", "tagTo", "username", "team", "city", "login_status", "last_update_time"])
        phone_expr = "RIGHT(REGEXP_REPLACE(COALESCE(" + ", ".join(f"{column}::text" for column in phone_columns) + ", ''), '\\D', '', 'g'), 10)"
        order_parts = []
        if "last_update_time" in columns:
            order_parts.append("last_update_time DESC NULLS LAST")
        if "source_id" in columns:
            order_parts.append("source_id DESC NULLS LAST")
        order_sql = "ORDER BY " + ", ".join(order_parts) if order_parts else ""
        rows = q(
            self.db,
            f"""
            SELECT {select_list}
            FROM {self._table_ref(table_name)}
            WHERE {phone_expr} = :phone10
            {order_sql}
            LIMIT 1
            """,
            {"phone10": phone10},
        )
        return rows[0] if rows else None

    def resolve(
        self,
        *,
        username: Any = None,
        phone: Any = None,
        email: Any = None,
        fallback_role: Optional[str] = None,
        fallback_actor: Any = None,
    ) -> Dict[str, Any]:
        cache_key = (
            str(username or "").strip().lower(),
            str(phone_last10(phone) or ""),
            str(norm_email(email) or ""),
            str(fallback_role or "").strip().lower(),
            str(fallback_actor or "").strip().lower(),
        )
        if cache_key in self._cache:
            return dict(self._cache[cache_key])

        row = self._lookup_user_account(username=username, email=email)
        if row:
            result = self._staff_result(row, source="staging_user_account", fallback_actor=fallback_actor or username or email, fallback_role=fallback_role)
            self._cache[cache_key] = result
            return dict(result)

        row = self._lookup_phone_assignment(phone)
        if row:
            # If the pooled line gives a username but no team, enrich from user account.
            enriched = self._lookup_user_account(username=row.get("username") or row.get("tag_to") or row.get("tagTo"))
            merged = dict(row)
            if enriched:
                merged.update({k: v for k, v in enriched.items() if v not in (None, "", [], {})})
            result = self._staff_result(merged, source="staging_staff_phone_assignment", fallback_actor=fallback_actor or phone, fallback_role=fallback_role)
            self._cache[cache_key] = result
            return dict(result)

        row = self._lookup_user_account(phone=phone)
        if row:
            result = self._staff_result(row, source="staging_user_account_phone", fallback_actor=fallback_actor or phone, fallback_role=fallback_role)
            self._cache[cache_key] = result
            return dict(result)

        result = self._staff_result(None, source="fallback", fallback_actor=fallback_actor or username or email or phone, fallback_role=fallback_role)
        self._cache[cache_key] = result
        return dict(result)


def now_ist_naive() -> datetime:
    return datetime.utcnow() + IST_OFFSET


def resolve_window(days: int) -> Tuple[datetime, datetime, str]:
    safe_days = 30 if days <= 0 else int(days)
    end_dt = now_ist_naive()
    start_dt = end_dt - timedelta(days=safe_days)
    return start_dt, end_dt, f"last {safe_days} day(s)"


def norm_digits(value: Any) -> str:
    return NON_DIGIT_RE.sub("", str(value or ""))


def phone_last10(value: Any) -> Optional[str]:
    digits = norm_digits(value)
    return digits[-10:] if len(digits) >= 10 else None


def show_phone(value: Any) -> str:
    p10 = phone_last10(value)
    return f"91{p10}" if p10 else ""


def norm_email(value: Any) -> Optional[str]:
    text_value = str(value or "").strip().lower()
    return text_value if "@" in text_value else None


def extract_emails(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    return sorted({m.group(0).lower() for m in EMAIL_RE.finditer(str(value))})


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


def flow_from_direction(direction: Any, actor_role: Optional[str] = None) -> str:
    d = normalize_direction(direction)
    role = normalize_actor_role(actor_role, fallback="company") or "company"
    if d in COMPANY_TO_CUSTOMER:
        return f"{role}_to_customer"
    if d in CUSTOMER_TO_COMPANY:
        return f"customer_to_{role}"
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


def text_for_message_type(message_type: Any, text_value: Any) -> str:
    txt = clean_text(text_value, 1000)
    if txt:
        return txt
    kind = message_kind(message_type, text_value)
    return "" if kind == "context" else f"[{kind}]" if kind in MEDIA_TYPES.values() else ""


def call_transcript_text(row: Dict[str, Any], max_len: int) -> str:
    return clean_text(
        row.get("translated_text")
        or row.get("transcript_text")
        or row.get("transcript_text_eleven_labs")
        or row.get("raw_transcripts"),
        max_len,
    )


def classify_text(text_value: str, flow: str, kind: str) -> str:
    text_l = (text_value or "").lower()
    for label, pattern in AUTOMATION_PATTERNS:
        if is_business_to_customer_flow(flow) and pattern.search(text_l):
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
        return "P1" if is_customer_to_business_flow(flow) else "P2"
    if channel == "travel_cart":
        return "P2"
    if is_customer_to_business_flow(flow) and category in {"manual_payment", "manual_support", "manual_booking", "manual_urgency"}:
        return "P1"
    if is_customer_to_business_flow(flow) and kind not in {"ack", "empty", "context"}:
        return "P2"
    if category.startswith("manual_") or category.startswith("media_"):
        return "P2"
    return "P4"




# ---------------------------------------------------------------------------
# Datetime parsing helper shared across modules
# ---------------------------------------------------------------------------
def parse_event_dt(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text_value = str(value).strip()
    if not text_value:
        return None
    try:
        return datetime.fromisoformat(text_value.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        try:
            return datetime.strptime(text_value[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None




# ---------------------------------------------------------------------------
# Lead review version/source constants
# ---------------------------------------------------------------------------
LEAD_REVIEW_CONTEXT_VERSION = "review_lead_communication:v10_lean_phase_context"
LEAD_REVIEW_SOURCE_SCOPE = "lead_id_plus_booking_confirm_and_lean_phase_context"
