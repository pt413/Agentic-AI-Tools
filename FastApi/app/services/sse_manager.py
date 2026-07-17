import asyncio
import json
import logging
from typing import Set, Optional

logger = logging.getLogger(__name__)


class SSEManager:
    def __init__(self):
        self.queues: Set[asyncio.Queue] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def connect(self) -> asyncio.Queue:
        self.loop = asyncio.get_running_loop()
        q = asyncio.Queue(maxsize=100)
        self.queues.add(q)
        return q

    def disconnect(self, q: asyncio.Queue):
        self.queues.discard(q)

    def broadcast_local(self, event_type: str, data: dict):
        """
        Local-only delivery to SSE clients connected to this FastAPI worker.
        Redis listener calls this.
        """
        if not self.queues:
            return

        sse_payload = f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"

        for q in list(self.queues):
            try:
                q.put_nowait(sse_payload)
            except asyncio.QueueFull:
                logger.warning("SSE queue full; dropping event for one client")
            except Exception:
                logger.exception("Error broadcasting SSE event locally")


sse_manager = SSEManager()
