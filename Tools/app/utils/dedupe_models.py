"""Data models for deduplication"""
from dataclasses import dataclass, asdict
import uuid
from datetime import datetime
import os
from app.dedupe_config import TZ

@dataclass
class ExecutionContext:
    batch_id: str
    worker_id: str
    started_at: str
    phase: str = "init"
    
    def to_dict(self):
        return asdict(self)

def create_execution_context(worker_id=None):
    ctx = ExecutionContext(
        batch_id=str(uuid.uuid4()),
        worker_id=worker_id or f"worker-{os.getpid()}",
        started_at=datetime.now(TZ).isoformat(),
    )
    return ctx
