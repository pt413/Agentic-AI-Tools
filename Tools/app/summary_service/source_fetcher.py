from sqlalchemy import create_engine, Table, MetaData, select
from db.database import DATABASE_URL
from datetime import datetime, timedelta

engine = create_engine(DATABASE_URL)
metadata = MetaData()

Messages = Table("messages", metadata, autoload_with=engine)


def normalize_number(num: str) -> str:
    if not num:
        return ""
    digits = "".join(filter(str.isdigit, num))
    return digits[-10:]


def get_whatsapp_conversations(cx_number: str, day: str):
    """
    Fetch all messages for a customer number from the messages table
    for the given day, grouped by admin_number.
    Returns a dict: {admin_number: conversation_text}
    """
    try:
        day_dt = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid date format: {day}. Use YYYY-MM-DD.")

    start_day = datetime(day_dt.year, day_dt.month, day_dt.day)
    end_day = start_day + timedelta(days=1)

    with engine.connect() as conn:
        stmt_msg = (
            select(Messages)
            .where(Messages.c.cx_number == cx_number)
            .where(Messages.c.timestamp >= start_day)
            .where(Messages.c.timestamp < end_day)
            .order_by(Messages.c.timestamp.asc())
        )
        rows = conn.execute(stmt_msg).mappings().all()

    # group messages by admin
    conversations = {}
    for row in rows:
        text = row['clean_content'] or row['content']
        if not text:
            continue

        direction = row['direction'].lower()
        admin_num = normalize_number(row.get('admin_number') or "unknown")

        if admin_num not in conversations:
            conversations[admin_num] = []

        if direction == "incoming":
            conversations[admin_num].append(f"customer: {text}")
        elif direction == "outgoing":
            conversations[admin_num].append(f"admin({admin_num}): {text}")
        else:
            conversations[admin_num].append(f"unknown: {text}")

    return {
        admin: "\n".join(parts)
        for admin, parts in conversations.items()
    }
