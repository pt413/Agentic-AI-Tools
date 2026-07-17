from dataclasses import dataclass
from typing import Optional, Iterable
from datetime import datetime

@dataclass
class Record:
    """
    Generic record produced by connectors.
    Fields mirror attributes used by your existing sync logic.
    """
    source_table: str
    source_id: int
    channel: str                 
    sender_raw: Optional[str]
    receiver_raw: Optional[str]
    content: Optional[str]
    timestamp: Optional[datetime]
    raw_obj: object              

class ConnectorBase:
    """
    Base connector interface.
    Concrete connectors must implement:
      - fetch_unsynced(session, batch_size: int, offset: int=0) -> Iterable[Record]
      - mark_synced(session, raw_obj, reason: Optional[str] = None) -> None

    NOTE: offset-based pagination is intentionally preserved to match existing sync_all_to_unified.py.
    """
    def fetch_unsynced(self, session, batch_size: int, offset: int = 0) -> Iterable[Record]:
        raise NotImplementedError

    def mark_synced(self, session, raw_obj, reason: Optional[str] = None) -> None:
        raise NotImplementedError
