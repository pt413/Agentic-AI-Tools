from typing import Iterable
from sqlalchemy import select, or_
from app.connectors.connector_base import ConnectorBase, Record
from app.model.audio_file_model import AudioFile
from app.utils.logger import get_logger

log = get_logger(__name__)
SOURCE_TABLE = "call_recordings_transcript"  

class AudioConnector(ConnectorBase):
    """
    Connector for call_recordings_transcript (AudioFile model).
    Uses fields:
      - emp_phone_number
      - customer_phone_number
      - transcript_text
      - call_datetime
      - sync_status
      - id
    Selection predicate preserves original: or_(sync_status != 1, sync_status == None)
    """
    def fetch_unsynced(self, session, batch_size: int, offset: int = 0) -> Iterable[Record]:
        q = select(AudioFile).where(
            or_(AudioFile.sync_status != 1, AudioFile.sync_status == None)
        ).order_by(AudioFile.id).offset(offset).limit(batch_size)
        rows = session.execute(q).scalars().all()
        log.debug("audio_fetch", extra={"count": len(rows), "offset": offset, "batch_size": batch_size})
        for r in rows:
            emp_phone = (r.emp_phone_number or "").strip() or None
            cus_phone = (r.customer_phone_number or "").strip() or None
            yield Record(
                source_table=SOURCE_TABLE,
                source_id=r.id,
                channel="call",
                sender_raw=emp_phone,
                receiver_raw=cus_phone,
                content=r.transcript_text,
                timestamp=r.call_datetime,
                raw_obj=r
            )

    def mark_synced(self, session, raw_obj, reason: str = None) -> None:
        
        raw_obj.sync_status = 1
        session.add(raw_obj)
        log.debug("audio_mark_synced", extra={"id": getattr(raw_obj, "id", None), "reason": reason})
