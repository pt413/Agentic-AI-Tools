from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.model.user_data import UserData
from app.utils.logger import get_logger

logger = get_logger("unify-creators")


def now_ist():
    return datetime.utcnow()


def get_or_create_by_phone(session: Session, phone: str):
    if not phone:
        return None

    phone = phone.strip()
    user = session.execute(
        select(UserData).where(UserData.phone == phone)
    ).scalars().first()

    if user:
        return user

    u = UserData(
        name=None,
        phone=phone,
        wa_num=None,
        email=None,
        role=None,
        creation_time=now_ist(),
        updation_time=now_ist(),
        org_id=""
    )
    session.add(u)

    try:
        session.flush()
        return u
    except IntegrityError:
        logger.warning("IntegrityError creating phone=%s, retrying", phone)
        session.rollback()
        return session.execute(
            select(UserData).where(UserData.phone == phone)
        ).scalars().first()


def get_or_create_by_wa(session: Session, wa_num: str):
    if not wa_num:
        return None

    wa = wa_num.strip()
    user = session.execute(
        select(UserData).where(UserData.wa_num == wa)
    ).scalars().first()

    if user:
        return user

    u = UserData(
        name=None,
        phone=None,
        wa_num=wa,
        email=None,
        role=None,
        creation_time=now_ist(),
        updation_time=now_ist(),
        org_id=""
    )
    session.add(u)

    try:
        session.flush()
        return u
    except IntegrityError:
        logger.warning("IntegrityError creating wa_num=%s, retrying", wa)
        session.rollback()
        return session.execute(
            select(UserData).where(UserData.wa_num == wa)
        ).scalars().first()


def get_or_create_by_email(session: Session, email: str):
    if not email:
        return None

    em = email.strip().lower()
    user = session.execute(
        select(UserData).where(UserData.email == em)
    ).scalars().first()

    if user:
        return user

    u = UserData(
        name=None,
        phone=None,
        wa_num=None,
        email=em,
        role=None,
        creation_time=now_ist(),
        updation_time=now_ist(),
        org_id=""
    )
    session.add(u)

    try:
        session.flush()
        return u
    except IntegrityError:
        logger.warning("IntegrityError creating email=%s, retrying", em)
        session.rollback()
        return session.execute(
            select(UserData).where(UserData.email == em)
        ).scalars().first()


def _is_outgoing(direction: str):
    if not direction:
        return None

    d = direction.strip().lower()

    if d.startswith("out") or d in ("sent", "sender", "outgoing"):
        return True

    if d.startswith("in") or d in ("received", "incoming", "inbound"):
        return False

    return None
