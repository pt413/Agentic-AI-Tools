from __future__ import annotations

from ..core.utils import extract_emails, normalize_email, normalize_event_direction, normalize_phone



def safe_event_end_time(event_time, end_time):
    if event_time is None or end_time is None:
        return None
    if end_time < event_time:
        return None
    return end_time



def normalize_message_direction(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in {"outgoing", "outbound", "sent", "from_admin", "reply"}:
        return "outbound"
    if text in {"incoming", "inbound", "received", "from_customer"}:
        return "inbound"
    normalized = normalize_event_direction(text)
    return normalized or text[:20]



def slugify_label(value: str | None, default: str = "unknown") -> str:
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    out = []
    last_was_sep = False
    for ch in text:
        if ch.isalnum():
            out.append(ch)
            last_was_sep = False
        else:
            if not last_was_sep:
                out.append("_")
                last_was_sep = True
    label = "".join(out).strip("_")
    return label or default



def append_contexts(processor, event_id, contexts):
    written = 0
    seen = set()
    for context_type, context_value in contexts:
        if context_value in (None, ""):
            continue
        item = (str(context_type), str(context_value))
        if item in seen:
            continue
        seen.add(item)
        context_id = processor.context.add_context(
            event_id=event_id,
            context_type=item[0],
            context_value=item[1],
        )
        written += 1 if context_id else 0
    return written



def add_phone_participant(
    processor,
    *,
    event_id,
    participant_seq,
    phone,
    role,
    source_table,
    source_id,
    event_time,
    direction_role=None,
):
    normalized = normalize_phone(phone)
    if not normalized:
        return participant_seq, 0

    participant_id = processor.participants.add_participant(
        event_id=event_id,
        participant_seq=participant_seq,
        key_type="phone",
        key_value=normalized,
        role=role,
        direction_role=direction_role,
        source_table=source_table,
        source_id=source_id,
        event_time=event_time,
    )
    return participant_seq + 1, (1 if participant_id else 0)



def add_email_participant(
    processor,
    *,
    event_id,
    participant_seq,
    email,
    role,
    source_table,
    source_id,
    event_time,
    direction_role=None,
):
    normalized = normalize_email(email)
    if not normalized:
        return participant_seq, 0

    participant_id = processor.participants.add_participant(
        event_id=event_id,
        participant_seq=participant_seq,
        key_type="email",
        key_value=normalized,
        role=role,
        direction_role=direction_role,
        source_table=source_table,
        source_id=source_id,
        event_time=event_time,
    )
    return participant_seq + 1, (1 if participant_id else 0)



def add_staff_participant(
    processor,
    *,
    event_id,
    participant_seq,
    staff_ref,
    role,
    source_table,
    source_id,
    event_time,
):
    if staff_ref in (None, ""):
        return participant_seq, 0

    value = str(staff_ref).strip()
    if not value:
        return participant_seq, 0

    if hasattr(processor, "source") and hasattr(processor.source, "clean_actor_ref"):
        value = processor.source.clean_actor_ref(value)
        if not value:
            return participant_seq, 0

    # Pre-seed staff identities as internal so fallback person creation
    # does not create unknown staff persons.
    if hasattr(processor, "identity"):
        processor.identity.resolve_or_create_person_from_keys(
            candidate_keys=[("staff_ref", value)],
            source_table=source_table,
            source_id=str(source_id),
            event_time=event_time,
            seed_fields={
                "canonical_name": value,
                "person_kind": "internal",
                "kind_confidence": 1,
            },
            merge_reason=f"{role}_staff_seed",
        )

    participant_id = processor.participants.add_participant(
        event_id=event_id,
        participant_seq=participant_seq,
        key_type="staff_ref",
        key_value=value,
        role=role,
        source_table=source_table,
        source_id=source_id,
        event_time=event_time,
    )
    return participant_seq + 1, (1 if participant_id else 0)


def add_email_string_participants(
    processor,
    *,
    event_id,
    participant_seq,
    raw_value,
    role,
    source_table,
    source_id,
    event_time,
    direction_role=None,
):
    written = 0
    seq = participant_seq
    for email in extract_emails(raw_value):
        seq, added = add_email_participant(
            processor,
            event_id=event_id,
            participant_seq=seq,
            email=email,
            role=role,
            source_table=source_table,
            source_id=source_id,
            event_time=event_time,
            direction_role=direction_role,
        )
        written += added
    return seq, written
