from typing import Dict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.utils.logger import get_logger

from app.connectors import AudioConnector, MessageConnector, EmailConnector
from app.persistence.user_service import (
    get_or_create_by_phone,
    get_or_create_by_wa,
    get_or_create_by_email,
)
from app.persistence.unified_service import create_unified_entry
from app.transforms.direction_parser import parse_direction
from app.transforms.normalizers import sanitize_content

from app.model.sales_data import SalesData

import re

log = get_logger(__name__)
COMPANY_DOMAIN = "rentmystay.com"



def normalize_phone(num: str | None) -> str | None:
    if not num:
        return None

    digits = re.sub(r"\D", "", num)

    if len(digits) >= 10:
        return digits[-10:]

    return None


def resolve_admin_name(session: Session, phone: str | None) -> str | None:
    phone = normalize_phone(phone)
    if not phone:
        return None

    return session.execute(
        select(SalesData.username)
        .where(SalesData.salesPhoneNumber == phone)
    ).scalar_one_or_none()


def resolve_admin_role(session: Session, phone: str | None) -> str | None:
    phone = normalize_phone(phone)
    if not phone:
        return None

    return session.execute(
        select(SalesData.admin_Team)
        .where(SalesData.salesPhoneNumber == phone)
    ).scalar_one_or_none()


CONNECTOR_MAP = {
    "call_recordings_transcript": AudioConnector(),
    "messages": MessageConnector(),
    "emails": EmailConnector(),
}


def _process_call(session: Session, af):
    emp_phone = normalize_phone(af.emp_phone_number)
    cus_phone = normalize_phone(af.customer_phone_number)

    emp_role = resolve_admin_role(session, emp_phone)
    emp_name = resolve_admin_name(session, emp_phone)

    emp_user = get_or_create_by_phone(
        session,
        emp_phone,
        role=emp_role,
        name=emp_name,
    )
    cus_user = get_or_create_by_phone(session, cus_phone, role="customer")

    outgoing = parse_direction(getattr(af, "call_type", None))
    if outgoing is True:
        sender_user, receiver_user = emp_user, cus_user
        sender_val, receiver_val = emp_phone, cus_phone
    elif outgoing is False:
        sender_user, receiver_user = cus_user, emp_user
        sender_val, receiver_val = cus_phone, emp_phone
    else:
        sender_user, receiver_user = emp_user, cus_user
        sender_val, receiver_val = emp_phone, cus_phone

    
    if not sender_user or not receiver_user:
        log.error("User mapping failed (call)", extra={"emp": emp_phone, "cus": cus_phone})
        return

    create_unified_entry(
        session=session,
        sen_id=sender_user.u_id,
        rec_id=receiver_user.u_id,
        sender=sender_val,
        receiver=receiver_val,
        channel="call",
        content=sanitize_content(af.transcript_text),
        timestamp=af.call_datetime,
        meta_data=None,
        embed_id=None,
    )

    af.sync_status = 1
    session.add(af)


def _process_message(session: Session, m):
    admin_num = normalize_phone(m.admin_number)
    cx_num = normalize_phone(m.cx_number)

    admin_role = resolve_admin_role(session, admin_num)
    admin_name = resolve_admin_name(session, admin_num)

    admin_user = get_or_create_by_wa(
        session,
        admin_num,
        role=admin_role,
        name=admin_name,
    )
    cx_user = get_or_create_by_wa(session, cx_num, role="customer")

    outgoing = parse_direction(getattr(m, "direction", None))
    if outgoing is True:
        sender_user, receiver_user = admin_user, cx_user
        sender_val, receiver_val = admin_num, cx_num
    elif outgoing is False:
        sender_user, receiver_user = cx_user, admin_user
        sender_val, receiver_val = cx_num, admin_num
    else:
        sender_user, receiver_user = admin_user, cx_user
        sender_val, receiver_val = admin_num, cx_num

    
    if not sender_user or not receiver_user:
        log.error("User mapping failed (message)", extra={
            "admin_num": admin_num,
            "cx_num": cx_num
        })
        return

    create_unified_entry(
        session=session,
        sen_id=sender_user.u_id,
        rec_id=receiver_user.u_id,
        sender=sender_val,
        receiver=receiver_val,
        channel="whatsapp",
        content=sanitize_content(m.clean_content),
        timestamp=m.timestamp,
        meta_data=None,
        embed_id=None,
    )

    m.sync_status = 1
    session.add(m)


def _process_email(session: Session, e):
    sender_role = "agent" if e.sender and COMPANY_DOMAIN in e.sender else "customer"
    receiver_role = "agent" if e.receiver and COMPANY_DOMAIN in e.receiver else "customer"

    sender_user = get_or_create_by_email(session, e.sender, role=sender_role)
    receiver_user = get_or_create_by_email(session, e.receiver, role=receiver_role)

    
    if not sender_user or not receiver_user:
        log.error("User mapping failed (email)", extra={"sender": e.sender, "receiver": e.receiver})
        return

    create_unified_entry(
        session=session,
        sen_id=sender_user.u_id,
        rec_id=receiver_user.u_id,
        sender=e.sender,
        receiver=e.receiver,
        channel="email",
        content=sanitize_content(e.body),
        timestamp=e.date,
        meta_data=None,
        embed_id=None,
    )

    e.sync_status = 1
    session.add(e)


_PROCESSOR_MAP = {
    "call_recordings_transcript": _process_call,
    "messages": _process_message,
    "emails": _process_email,
}


def run_sync_once(batch_size: int = 10) -> Dict[str, int]:
    session = SessionLocal()
    summary = {k: 0 for k in CONNECTOR_MAP}
    try:
        for source_table, connector in CONNECTOR_MAP.items():
            offset = 0
            while True:
                rows = list(connector.fetch_unsynced(session, batch_size, offset))
                if not rows:
                    break
                for rec in rows:
                    try:
                        with session.begin_nested():
                            _PROCESSOR_MAP[rec.source_table](session, rec.raw_obj)
                        session.commit()
                        summary[source_table] += 1
                    except Exception as exc:
                        session.rollback()
                        log.exception("record_processing_failed", exc_info=exc)
                offset += batch_size
    finally:
        session.close()
    return summary


if __name__ == "__main__":
    summary = run_sync_once(batch_size=10)
    print("LOCAL SYNC SUMMARY:", summary)