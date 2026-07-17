from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Optional

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, model_validator


# Several capability scripts still use script-style sibling imports. Route files
# live in app/routes, so resolve the project root and capability dir once here.
def _project_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent, *current.parents]:
        if (parent / "app").is_dir():
            return parent
    raise RuntimeError("Could not find FastApi project root containing app/.")


PROJECT_ROOT = _project_root()
CAPABILITIES_DIR = PROJECT_ROOT / "app" / "services" / "analytics_engine" / "capabilities"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(CAPABILITIES_DIR) not in sys.path:
    sys.path.insert(0, str(CAPABILITIES_DIR))

import app.services.analytics_engine.capabilities.review_number_communication as number_review  # noqa: E402


DEFAULT_SCHEMA = "AnalyticsEngine"


class CapabilityBaseModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class SeedMixin(CapabilityBaseModel):
    """Generic seed mixin kept only for timeline/number capabilities.

    Customer Brief no longer uses this because it must be booking_id-only to
    avoid broad identity expansion and unrelated-number matches.
    """

    user_id: Optional[int] = None
    booking_id: Optional[int] = None
    lead_id: Optional[int] = None
    person_id: Optional[int] = None
    email: Optional[str] = None
    phone: Optional[str] = None

    @model_validator(mode="after")
    def require_single_seed(self):
        active = [
            name
            for name in ("user_id", "booking_id", "lead_id", "person_id", "email", "phone")
            if getattr(self, name, None) not in (None, "")
        ]
        if len(active) != 1:
            raise ValueError(
                "Provide exactly one of user_id, booking_id, lead_id, person_id, email, or phone. "
                f"Got: {active or 'none'}"
            )
        return self

    def seed_kwargs(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "booking_id": self.booking_id,
            "lead_id": self.lead_id,
            "person_id": self.person_id,
            "email": self.email,
            "phone": self.phone,
        }


class TimelineIdentityRequest(SeedMixin):
    schema_name: str = Field(default=DEFAULT_SCHEMA, alias="schema")


class TimelineFactsRequest(SeedMixin):
    schema_name: str = Field(default=DEFAULT_SCHEMA, alias="schema")
    mode: Literal["active", "all"] = "active"
    entities: str = Field(default="all", description="all or comma-separated subset: lead,booking,ticket,invoice,property")


class TimelineConversationRequest(SeedMixin):
    schema_name: str = Field(default=DEFAULT_SCHEMA, alias="schema")
    channel: Literal["any", "whatsapp", "email", "call"] = "any"
    days: int = Field(default=7, ge=1, le=365)


class CustomerBriefRequest(CapabilityBaseModel):
    # Booking-only by design. Do not add lead_id/user_id/person_id/email/phone here.
    booking_id: int = Field(..., description="Booking ID. This is the only supported customer brief seed.")
    schema_name: str = Field(default=DEFAULT_SCHEMA, alias="schema")
    days: int = Field(default=30, ge=1, le=365)
    # Legacy output_format is still accepted for older callers. New UI uses display_mode.
    output_format: Literal["full", "llm", "both", "unrestricted"] = "llm"
    display_mode: Optional[Literal["evidence", "llm", "raw", "unrestricted"]] = Field(
        default=None,
        description=(
            "New UI mode. evidence is user-friendly; llm is copy-ready context; "
            "raw is full debug payload; unrestricted returns full conversation text and call transcripts."
        ),
    )
    llm: bool = True
    print_limit: int = Field(default=200, ge=0, le=5000)
    max_text: int = Field(default=220, ge=40, le=2000)
    max_llm_messages: int = Field(default=12, ge=1, le=50)
    max_llm_text_chars: int = Field(default=220, ge=40, le=2000)
    verbose: bool = False


class ActiveBookingRecentActivityRequest(CapabilityBaseModel):
    schema_name: str = Field(default=DEFAULT_SCHEMA, alias="schema")
    days: int = Field(default=3, ge=1, le=365)
    current_only: bool = Field(
        default=True,
        description=(
            "Backward-compatible flag. Active booking tracker now always uses current active RMS Prop stays: "
            "booking_status=success and travel_from_date <= today <= travel_to_date."
        ),
    )
    limit: int = Field(default=500, ge=1, le=5000)
    event_limit: int = Field(default=10000, ge=1, le=100000)
    max_events_per_booking: int = Field(default=5, ge=0, le=50)
    max_text: int = Field(default=180, ge=40, le=2000)
    include_timeline: bool = False
    ids_only: bool = Field(default=True, description="Return only distinct booking IDs and activity source counts.")
    include_contact_scans: bool = Field(default=False, description="Scan contact-linked call/WhatsApp/email sources only when explicitly enabled. Keep false for faster ID-only scan.")
    debug: bool = Field(default=False, description="Include per-step timing/debug information.")


class BookingFollowupReviewRequest(CapabilityBaseModel):
    schema_name: str = Field(default=DEFAULT_SCHEMA, alias="schema")
    booking_ids: Optional[str] = Field(default=None, description="Comma-separated booking IDs. Empty means discover from recent activity.")
    from_recent_activity: bool = Field(default=True)
    recent_days: int = Field(default=7, ge=1, le=365)
    customer_days: int = Field(default=30, ge=1, le=365)
    limit: int = Field(default=500, ge=1, le=5000)
    max_bookings: int = Field(default=25, ge=1, le=200)
    include_contact_scans: bool = Field(default=False)
    run_llm: bool = Field(default=True)
    model: str = Field(default="gpt-5-mini")
    max_llm_messages: int = Field(default=12, ge=1, le=50)
    max_llm_text_chars: int = Field(default=220, ge=40, le=2000)
    timeout_seconds: int = Field(default=120, ge=10, le=600)
    debug: bool = False


class LeadCommunicationReviewRequest(CapabilityBaseModel):
    lead_id: int
    schema_name: str = Field(default=DEFAULT_SCHEMA, alias="schema")
    days: int = Field(default=30, ge=1, le=365)
    limit: int = Field(default=10000, ge=1, le=100000)
    max_text: int = Field(default=180, ge=40, le=2000)
    print_limit: int = Field(default=200, ge=0, le=5000)
    llm: bool = True
    display_mode: Literal["evidence", "llm", "raw", "unrestricted"] = "evidence"


class NumberCommunicationReviewRequest(CapabilityBaseModel):
    # Phone-only by design. Do not add number/booking_id/lead_id/user_id/person_id/email here.
    schema_name: str = Field(default=DEFAULT_SCHEMA, alias="schema")
    phone: str = Field(..., description="Phone. Accepts 10 digits or 12 digits; matching uses last 10 digits.")
    days: int = Field(default=7, ge=1, le=365)
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    limit: int = Field(default=10000, ge=1, le=100000)
    max_text: int = Field(default=220, ge=40, le=2000)
    print_limit: int = Field(default=200, ge=0, le=10000)
    llm: bool = True
    role: Literal["any", "admin", "counterparty"] = "any"
    view: Literal["clean", "raw"] = "clean"
    focus: Literal["all", "review", "manual", "automation"] = "all"
    hide_automation: bool = False
    display_mode: Literal["evidence", "llm", "raw"] = "evidence"

    @model_validator(mode="after")
    def require_phone_only(self):
        phone_p10 = number_review.phone_last10(self.phone)
        if not phone_p10:
            raise ValueError("Provide phone with 10 digits or 12 digits. Matching uses last 10 digits.")
        return self


class WhatsAppInspectRequest(CapabilityBaseModel):
    schema_name: str = Field(default=DEFAULT_SCHEMA, alias="schema")
    phone: Optional[str] = None
    actor: Optional[str] = None
    lead_id: Optional[int] = None
    thread_key: Optional[str] = None
    remote_jid: Optional[str] = None
    admin_number: Optional[str] = None
    customer_number: Optional[str] = None
    list_threads: bool = False
    include_overall_summary: bool = False
    limit: int = Field(default=120, ge=1, le=5000)


class StaffActivityReviewRequest(CapabilityBaseModel):
    # Staff activity is staff-only. Do not add booking/lead/customer seeds here.
    # role defaults to auto. StaffActivityReviewService resolves actual scope from
    # staging_user_account.team when available.
    schema_name: str = Field(default=DEFAULT_SCHEMA, alias="schema")

    username: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = Field(default=None, description="Staff phone. Accepts 10 or 12 digits; matching uses last 10 digits.")

    role: str = Field(
        default="auto",
        description="Default auto uses staging_user_account.team. Request role is fallback only when staff team is missing/unknown.",
    )

    days: int = Field(default=3, ge=1, le=365)
    limit: int = Field(default=10000, ge=1, le=100000)
    print_limit: int = Field(default=50, ge=0, le=5000)
    max_text: int = Field(default=160, ge=40, le=2000)
    llm: bool = True
    display_mode: Literal["evidence", "llm", "raw"] = "evidence"

    @model_validator(mode="after")
    def require_single_staff_seed(self):
        active = [
            name
            for name in ("username", "email", "phone")
            if getattr(self, name, None) not in (None, "")
        ]
        if len(active) != 1:
            raise ValueError(f"Provide exactly one of username, email, or phone. Got: {active or 'none'}")

        if self.phone not in (None, "") and not number_review.phone_last10(self.phone):
            raise ValueError("Provide staff phone with 10 or 12 digits. Matching uses last 10 digits.")

        return self


def _api_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": exc.__class__.__name__, "detail": str(exc)})


def _encode(payload: Any) -> Any:
    return jsonable_encoder(payload, custom_encoder={datetime: lambda v: v.isoformat(), date: lambda v: v.isoformat()})


def _namespace(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


def _json_pretty(value: Any) -> str:
    return json.dumps(_encode(value), ensure_ascii=False, indent=2, default=str)


def _build_communication_analysis_prompt(*, title: str, payload: Any) -> str:
    data_text = payload if isinstance(payload, str) else _json_pretty(payload)
    return f"""Below is the communication and customer/lead context from AnalyticsEngine for: {title}.

Please analyse this communication as a customer-handling quality reviewer.

Focus on:
1. Overall communication quality and whether the handling would improve or reduce customer satisfaction.
2. Timeline of important events, unanswered messages, delays, repeated follow-ups, and missed calls.
3. Executive/admin performance: responsiveness, clarity, empathy, ownership, accuracy, and closure quality.
4. Customer sentiment and risk level: frustration, urgency, escalation risk, payment/booking/support risk.
5. Process gaps: missing handoffs, automation noise, unclear ownership, wrong or incomplete responses.
6. What was handled correctly.
7. What should be improved immediately.
8. Give a rating out of 10 with clear reasons.
9. Suggest exact next best actions and a better response message if follow-up is needed.

Keep the analysis structured, practical, and concise. Avoid generic advice. Use evidence from the communication data below.
You MUST end your response with this exact line (replace X with your score):
RATING: X/10 — <one sentence reason>

COMMUNICATION DATA:
{data_text}
""".strip()




def _build_customer_brief_analysis_prompt(*, title: str, payload: Any) -> str:
    data_text = payload if isinstance(payload, str) else _json_pretty(payload)
    return f"""Act as RentMyStay customer success + operations quality reviewer.

Goal: judge how well this booking/customer was handled, not just summarize. Review support quality, ownership, response quality, open issues, ticket closure/time-to-close, communication handling, and customer risk.

Use only CUSTOMER BRIEF DATA. Do not invent facts; if missing, say "not visible in provided data".
Rules:
- Ignore finance/invoice/deposit/refund/payment correctness unless the customer clearly raised it in communication text.
- Treat automated emails separately from human follow-up.
- Do not assume a ticket is resolved unless closed or customer-confirmed.
- Keep concise and evidence-based; cite plain evidence such as ticket id/age, call time, missed call, role, channel, or subject.

Return exactly:
1. Overall verdict: score /10, main reason.
2. Booking health: state, stay dates, onboarding/check-in risk, support adequacy.
3. Support handling: open/closed tickets, age/time-to-close, unresolved issues, SLA/ownership gaps, closure quality.
4. Communication handling: key touches, missed calls/delays, follow-ups, handoffs, automation noise, right team/role.
5. Customer sentiment and risk: visible frustration/urgency, inferred risk, escalation/trust gap.
6. Score reasons: 3-5 bullets.
7. Immediate action needed today: table Priority | Owner/team | Action | Evidence; only evidence-backed actions.
8. What was handled well.
9. Next-best actions.
10. Customer follow-up: write a short message if follow-up is needed; otherwise write "No customer follow-up needed based on provided evidence."

Keep the analysis practical and evidence-based. Do not invent facts that are not in the data.
You MUST end your response with this exact line (replace X with your score):
RATING: X/10 — <one sentence reason>
CUSTOMER BRIEF DATA:
{data_text}
""".strip()

def _brief_amount(value: Any) -> str:
    if value in (None, "", [], {}):
        return "-"
    try:
        number = float(value)
        if number.is_integer():
            return f"₹{int(number):,}"
        return f"₹{number:,.2f}"
    except Exception:
        return str(value)


def _brief_value(value: Any) -> str:
    if value in (None, "", [], {}):
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value if item not in (None, "", [], {})) or "-"
    return str(value)


def _brief_section(title: str, items: list[tuple[str, Any]], rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    section = {
        "title": title,
        "items": [
            {"label": label, "value": _brief_value(value)}
            for label, value in items
            if value not in (None, "", [], {})
        ],
    }
    if rows:
        section["rows"] = rows
    return section


def _customer_brief_summary_text(evidence: dict[str, Any]) -> str:
    lines: list[str] = [str(evidence.get("title") or "Customer Brief")]
    booking_id = evidence.get("booking_id")
    if booking_id:
        lines.append(f"Booking ID: {booking_id}")

    for card in evidence.get("summary_cards") or []:
        label = card.get("label")
        value = card.get("value")
        if label and value not in (None, ""):
            lines.append(f"{label}: {value}")

    for section in evidence.get("sections") or []:
        title = section.get("title")
        if title:
            lines.append("")
            lines.append(str(title))
        for item in section.get("items") or []:
            if item.get("label") and item.get("value") not in (None, ""):
                lines.append(f"- {item['label']}: {item['value']}")
        rows = section.get("rows") or []
        if rows:
            lines.append("  Rows:")
            for row in rows[:12]:
                row_bits = []
                for key, value in row.items():
                    if value in (None, "", [], {}):
                        continue
                    row_bits.append(f"{key}={value}")
                if row_bits:
                    lines.append("  - " + "; ".join(row_bits))

    timeline_text = ((evidence.get("copy_blocks") or {}).get("timeline_text") or "").strip()
    if timeline_text:
        lines.append("")
        lines.append("Conversation timeline")
        lines.append(timeline_text)
    return "\n".join(lines).strip()


def _build_customer_brief_evidence(
    *,
    booking_id: int,
    full_payload: dict[str, Any],
    llm_context: dict[str, Any],
    llm_prompt: str | None,
    print_limit: int,
    max_text: int,
) -> dict[str, Any]:
    customer = full_payload.get("customer") or {}
    ids = customer.get("ids") or {}
    contacts_payload = customer.get("contacts") or {}
    contact_sources = customer.get("contact_sources") or []
    booking = llm_context.get("booking") or {}
    support = llm_context.get("support") or {}
    conversation = llm_context.get("conversation") or {}
    facts = full_payload.get("facts") or {}
    full_conversation = full_payload.get("conversation") or {}
    rows = list(full_conversation.get("recent_messages") or [])

    conv_stats = conversation.get("stats") or {}
    call_stats = conv_stats.get("calls") or {}
    channels = conv_stats.get("by_channel") or {}

    booking_card_parts = [
        f"id={booking.get('id') or booking_id}",
        booking.get("status"),
        booking.get("state"),
        booking.get("stay"),
    ]
    booking_card = " | ".join(str(part) for part in booking_card_parts if part not in (None, "", [], {}))

    conversation_card = (
        f"{conversation.get('count', 0)} events"
        f" | calls connected={call_stats.get('connected', 0)}, "
        f"missed={call_stats.get('missed', 0)}, "
        f"customer_not_answered={call_stats.get('customer_not_answered', 0)}"
        f" | channels={', '.join(f'{k}:{v}' for k, v in channels.items()) if channels else '-'}"
    )
    support_card = f"open={support.get('open_ticket_count', 0)} / total={support.get('total_ticket_count', 0)}"

    summary_cards = [
        {"label": "Booking", "value": booking_card},
        {"label": "Support", "value": support_card},
        {"label": "Conversation", "value": conversation_card},
    ]

    sections = [
        _brief_section(
            "Open tickets",
            [],
            rows=(support.get("open_tickets") or [])[:10],
        ),
        _brief_section(
            "Closed tickets",
            [],
            rows=(support.get("closed_tickets") or [])[:10],
        ),
        _brief_section(
            "Communication stats",
            [
                ("Last message", conversation.get("last_message_at")),
                ("Top agents", conv_stats.get("top_agents")),
                ("Flows", conv_stats.get("by_flow")),
            ],
        ),
    ]

    evidence = _build_evidence_payload(
        title="Customer Brief",
        identity={"booking_id": booking_id},
        window=(conversation.get("window") or full_conversation.get("window") or {}),
        contacts={
            "phones": contacts_payload.get("phones"),
            "emails": contacts_payload.get("emails"),
        },
        resolved_universe={},
        rows=rows,
        print_limit=print_limit,
        max_text=max_text,
        extra={
            "source_scope": "booking_id_only",
            "summary_cards": summary_cards,
            "sections": sections,
            "booking": booking,
            "support": support,
        },
    )
    if llm_prompt:
        evidence["llm_prompt"] = llm_prompt
    evidence["copy_blocks"] = dict(evidence.get("copy_blocks") or {})
    evidence["copy_blocks"]["customer_brief_text"] = _customer_brief_summary_text(evidence)
    return evidence

def _with_copy_blocks(result: dict[str, Any], *, prompt_key: str = "llm_prompt") -> dict[str, Any]:
    # Keep raw response lean. Do not duplicate llm_prompt or full response JSON
    # inside copy_blocks; the UI can copy those directly from the response.
    result["copy_blocks"] = dict(result.get("copy_blocks") or {})
    return result


def _event_time(row: dict[str, Any]) -> Any:
    return row.get("time") or row.get("event_time") or row.get("call_time") or row.get("message_time") or row.get("email_date")


def _event_flow(row: dict[str, Any]) -> str:
    return str(row.get("flow") or row.get("role_flow") or row.get("business_flow") or "unknown").strip() or "unknown"


def _compact_event_row(row: dict[str, Any], *, max_text: int = 240) -> dict[str, Any]:
    text_value = str(row.get("text") or row.get("message") or row.get("body") or row.get("snippet") or "").strip()
    subject_value = str(row.get("subject") or "").strip()
    transcript_value = str(row.get("transcript") or "").strip()
    if subject_value and text_value and subject_value.lower() not in text_value.lower():
        text_value = f"{subject_value}: {text_value}"
    elif subject_value and not text_value:
        text_value = subject_value
    if transcript_value and transcript_value != text_value:
        text_value = (text_value + "\n" if text_value else "") + f"Transcript: {transcript_value}"
    if max_text and len(text_value) > max_text:
        text_value = text_value[: max_text - 3].rstrip() + "..."

    compact = {
        "time": _event_time(row),
        "channel": row.get("channel"),
        "flow": _event_flow(row),
        "status": row.get("status"),
        "actor": row.get("actor") or row.get("actor_name") or row.get("executive") or row.get("executive_id") or row.get("agent"),
        "role": row.get("actor_role") or row.get("agent_role") or row.get("role"),
        "duration_sec": row.get("duration_sec"),
        "text": text_value,
        "transcript": transcript_value,
    }
    compact = {k: v for k, v in compact.items() if v not in (None, "", [], {})}

    parts = [str(compact.get("time") or ""), str(compact.get("channel") or ""), str(compact.get("flow") or "")]
    details: list[str] = []
    if compact.get("status"):
        details.append(str(compact["status"]))
    if compact.get("duration_sec") not in (None, ""):
        details.append(f"{compact['duration_sec']}s")
    if compact.get("role"):
        details.append(f"role={compact['role']}")
    if compact.get("actor"):
        details.append(f"actor={compact['actor']}")
    if details:
        parts.append(", ".join(details))
    if text_value:
        parts.append(text_value)
    compact["line"] = " | ".join(part for part in parts if part)
    return compact

def _communication_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    channels: dict[str, int] = {}
    flows: dict[str, int] = {}
    calls = {"total": 0, "connected": 0, "missed": 0, "customer_not_answered": 0, "talk_time_sec": 0}

    for row in rows:
        channel = str(row.get("channel") or "unknown").strip().lower() or "unknown"
        flow = _event_flow(row).lower()
        channels[channel] = channels.get(channel, 0) + 1
        flows[flow] = flows.get(flow, 0) + 1

        if channel == "call":
            calls["total"] += 1
            status = str(row.get("status") or "").strip().lower()
            try:
                duration = int(row.get("duration_sec") or 0)
            except Exception:
                duration = 0
            calls["talk_time_sec"] += duration
            if status == "connected" or duration > 0:
                calls["connected"] += 1
            elif status:
                calls["missed"] += 1
            elif status == "customer_not_answered":
                calls["customer_not_answered"] += 1

    return {
        "rows": len(rows),
        "channels": dict(sorted(channels.items())),
        "flows": dict(sorted(flows.items())),
        "calls": calls,
    }


def _timeline_copy_text(rows: list[dict[str, Any]], *, max_rows: int = 500) -> str:
    lines: list[str] = []
    for row in rows[:max_rows]:
        compact = _compact_event_row(row, max_text=500)
        line = str(compact.get("line") or "").strip()
        if line:
            lines.append(line)
    return "\n".join(lines)

def _build_evidence_payload(
    *,
    title: str,
    identity: dict[str, Any],
    window: dict[str, Any],
    contacts: dict[str, Any] | None = None,
    resolved_universe: dict[str, Any] | None = None,
    rows: list[dict[str, Any]],
    print_limit: int = 200,
    max_text: int = 240,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    visible_rows = rows[:print_limit] if print_limit else rows
    evidence = {
        "view": "evidence",
        "title": title,
        **identity,
        "window": window,
        "contacts": contacts or {},
        "resolved_universe": resolved_universe or {},
        "metrics": _communication_metrics(rows),
        "row_count": len(rows),
        "timeline": [_compact_event_row(row, max_text=max_text) for row in visible_rows],
    }
    if extra:
        evidence.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
    evidence["copy_blocks"] = {"timeline_text": _timeline_copy_text(visible_rows)}
    return evidence


