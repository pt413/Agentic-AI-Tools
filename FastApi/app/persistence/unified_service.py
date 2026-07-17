from sqlalchemy.orm import Session
from app.model.uni_activity import UnifiedData
from app.utils.logger import get_logger

log = get_logger(__name__)

def create_unified_entry(session: Session,
                         sen_id,
                         rec_id,
                         sender,
                         receiver,
                         channel,
                         content,
                         timestamp,
                         meta_data=None,
                         embed_id=None):
    """
    Creates a UnifiedData entry with the fields used in your original script.
    This preserves the exact column names and behaviour: it simply constructs and
    adds a UnifiedData model and flushes the session (no additional uniqueness checks).
    This mirrors the original `UnifiedData(...)` usage in sync_all_to_unified.py.
    """
    u = UnifiedData(
        sen_id=sen_id,
        rec_id=rec_id,
        sender=sender,
        receiver=receiver,
        channel=channel,
        content=content,
        timestamp=timestamp,
        meta_data=meta_data,
        embed_id=embed_id
    )
    session.add(u)
    session.flush()
    log.debug("created_unified", extra={"source_sender": sender, "source_receiver": receiver, "id": getattr(u, "id", None)})
    return u
