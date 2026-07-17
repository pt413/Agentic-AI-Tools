from typing import Iterable
from sqlalchemy import select, or_
from app.connectors.connector_base import ConnectorBase, Record
from app.model.emails import Email
from app.utils.logger import get_logger

log = get_logger(__name__)
SOURCE_TABLE = "emails"  

class EmailConnector(ConnectorBase):
    """
    Connector for emails (Email model).
    Uses fields:
      - sender
      - receiver
      - body
      - date
      - sync_status (integer)
      - id
    Selection predicate preserves original: or_(sync_status != 1, sync_status == None)
    """
    def fetch_unsynced(self, session, batch_size: int, offset: int = 0) -> Iterable[Record]:
        q = select(Email).where(
            or_(Email.sync_status != 1, Email.sync_status == None)
        ).order_by(Email.id).offset(offset).limit(batch_size)
        rows = session.execute(q).scalars().all()
        log.debug("email_fetch", extra={"count": len(rows), "offset": offset, "batch_size": batch_size})
        for r in rows:
            sender_em = (r.sender or "").strip().lower() or None
            receiver_em = (r.receiver or "").strip().lower() or None
            yield Record(
                source_table=SOURCE_TABLE,
                source_id=r.id,
                channel="email",
                sender_raw=sender_em,
                receiver_raw=receiver_em,
                content=r.body,
                timestamp=r.date,
                raw_obj=r
            )

    def mark_synced(self, session, raw_obj, reason: str = None) -> None:
        raw_obj.sync_status = 1
        session.add(raw_obj)
        log.debug("email_mark_synced", extra={"id": getattr(raw_obj, "id", None), "reason": reason})
