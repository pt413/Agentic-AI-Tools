from typing import Iterable
from sqlalchemy import select, or_
from app.connectors.connector_base import ConnectorBase, Record
from app.model.message import Message
from app.utils.logger import get_logger

log = get_logger(__name__)
SOURCE_TABLE = "messages"  

class MessageConnector(ConnectorBase):
    """
    Connector for messages (Message model).
    Uses fields:
      - admin_number
      - cx_number
      - clean_content
      - direction
      - timestamp
      - synced (flag)
      - id
    Selection predicate preserves original: or_(synced != True, synced == None)
    """
    def fetch_unsynced(self, session, batch_size: int, offset: int = 0) -> Iterable[Record]:
        q = select(Message).where(
            or_(Message.sync_status != 1, Message.sync_status == None)
        ).order_by(Message.id).offset(offset).limit(batch_size)
        rows = session.execute(q).scalars().all()
        log.debug("message_fetch", extra={"count": len(rows), "offset": offset, "batch_size": batch_size})
        for r in rows:
            admin_num = (r.admin_number or "").strip() or None
            cx_num = (r.cx_number or "").strip() or None
            
            yield Record(
                source_table=SOURCE_TABLE,
                source_id=r.id,
                channel="whatsapp",
                sender_raw=admin_num,
                receiver_raw=cx_num,
                content=r.clean_content,
                timestamp=r.timestamp,
                raw_obj=r
            )

    def mark_synced(self, session, raw_obj, reason: str = None) -> None:
        raw_obj.sync_status = 1
        session.add(raw_obj)
        log.debug("message_mark_synced", extra={"id": getattr(raw_obj, "id", None), "reason": reason})
