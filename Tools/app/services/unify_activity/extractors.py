from sqlalchemy import select, or_
from app.db.database import SessionLocal
from app.model.audio_file_model import AudioFile
from app.model.message import Message
from app.model.emails import Email
from app.utils.logger import get_logger

logger = get_logger("unify-extractors")


def fetch_calls(batch_size: int = 10):
    session = SessionLocal()
    try:
        offset = 0
        while True:
            rows = session.execute(
                select(AudioFile)
                .where(or_(AudioFile.sync_status != 1, AudioFile.sync_status == None))
                .order_by(AudioFile.id)
                .offset(offset)
                .limit(batch_size)
            ).scalars().all()

            if not rows:
                break

            logger.info("Fetched %s call records", len(rows))
            yield rows
            offset += batch_size

    finally:
        session.close()


def fetch_messages(batch_size: int = 10):
    session = SessionLocal()
    try:
        offset = 0
        while True:
            rows = session.execute(
                select(Message)
                .where(or_(Message.synced != True, Message.synced == None))
                .order_by(Message.id)
                .offset(offset)
                .limit(batch_size)
            ).scalars().all()

            if not rows:
                break

            logger.info("Fetched %s message records", len(rows))
            yield rows
            offset += batch_size

    finally:
        session.close()


def fetch_emails(batch_size: int = 10):
    session = SessionLocal()
    try:
        offset = 0
        while True:
            rows = session.execute(
                select(Email)
                .where(or_(Email.sync_status != 1, Email.sync_status == None))
                .order_by(Email.id)
                .offset(offset)
                .limit(batch_size)
            ).scalars().all()

            if not rows:
                break

            logger.info("Fetched %s email records", len(rows))
            yield rows
            offset += batch_size

    finally:
        session.close()
