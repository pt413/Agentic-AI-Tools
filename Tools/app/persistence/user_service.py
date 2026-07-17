from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from zoneinfo import ZoneInfo

from app.model.user_data import UserData
from app.utils.logger import get_logger

log = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")


def now_ist():
    return datetime.now(tz=IST)


def _update_role_if_missing(session: Session, user: UserData, role: str | None):
    if not role:
        return user

    if not user.role:
        user.role = role
        user.updation_time = now_ist()
        session.add(user)
        log.info("ROLE_SET", extra={"u_id": user.u_id, "role": role})

    return user


def _update_name_if_missing(session: Session, user: UserData, name: str | None):
    if not name:
        return user

    if not user.name:
        user.name = name
        user.updation_time = now_ist()
        session.add(user)
        log.info("NAME_SET", extra={"u_id": user.u_id, "user_name": name})

    return user


def get_or_create_by_phone(
    session: Session,
    phone: str | None,
    role: str | None = None,
    name: str | None = None,
):
    if not phone:
        return None

    phone = phone.strip()
    log.info("CHECK_BEFORE_INSERT", extra={"phone": phone})

    user = session.execute(
        select(UserData).where(UserData.phone == phone)
    ).scalars().first()

    if user:
        user = _update_role_if_missing(session, user, role)
        user = _update_name_if_missing(session, user, name)
        return user

    u = UserData(
        name=name,
        phone=phone,
        wa_num=None,
        email=None,
        role=role,
        creation_time=now_ist(),
        updation_time=now_ist(),
        org_id="",
    )
    session.add(u)

    try:
        session.flush()
        log.debug(
            "CREATED_USER_PHONE",
            extra={"u_id": u.u_id, "phone": phone, "role": role, "name": name},
        )
        return u
    except IntegrityError:
        session.rollback()
        log.warning("DUPLICATE_BLOCKED", extra={"phone": phone})
        user = session.execute(
            select(UserData).where(UserData.phone == phone)
        ).scalars().first()
        user = _update_role_if_missing(session, user, role)
        user = _update_name_if_missing(session, user, name)
        return user


def get_or_create_by_wa(
    session: Session,
    wa_num: str | None,
    role: str | None = None,
    name: str | None = None,
):
    if not wa_num:
        return None

    wa = wa_num.strip()
    log.info("CHECK_BEFORE_INSERT", extra={"wa_num": wa})

    user = session.execute(
        select(UserData).where(UserData.wa_num == wa)
    ).scalars().first()

    if user:
        user = _update_role_if_missing(session, user, role)
        user = _update_name_if_missing(session, user, name)
        return user

    u = UserData(
        name=name,
        phone=None,
        wa_num=wa,
        email=None,
        role=role,
        creation_time=now_ist(),
        updation_time=now_ist(),
        org_id="",
    )
    session.add(u)

    try:
        session.flush()
        log.debug(
            "CREATED_USER_WA",
            extra={"u_id": u.u_id, "wa": wa, "role": role, "name": name},
        )
        return u
    except IntegrityError:
        session.rollback()
        log.warning("DUPLICATE_BLOCKED", extra={"wa_num": wa})
        user = session.execute(
            select(UserData).where(UserData.wa_num == wa)
        ).scalars().first()
        user = _update_role_if_missing(session, user, role)
        user = _update_name_if_missing(session, user, name)
        return user


def get_or_create_by_email(
    session: Session,
    email: str | None,
    role: str | None = None,
    name: str | None = None,
):
    if not email:
        return None

    em = email.strip().lower()
    log.info("CHECK_BEFORE_INSERT", extra={"email": em})

    user = session.execute(
        select(UserData).where(UserData.email == em)
    ).scalars().first()

    if user:
        user = _update_role_if_missing(session, user, role)
        user = _update_name_if_missing(session, user, name)
        return user

    u = UserData(
        name=name,
        phone=None,
        wa_num=None,
        email=em,
        role=role,
        creation_time=now_ist(),
        updation_time=now_ist(),
        org_id="",
    )
    session.add(u)

    try:
        session.flush()

        log.debug(
            "CREATED_USER_EMAIL",
            extra={"u_id": u.u_id, "email": em, "role": role, "name": name},
        )
        return u
    except IntegrityError:
        session.rollback()
        log.warning("DUPLICATE_BLOCKED", extra={"email": em})
        user = session.execute(
            select(UserData).where(UserData.email == em)
        ).scalars().first()
        user = _update_role_if_missing(session, user, role)
        user = _update_name_if_missing(session, user, name)
        return user
