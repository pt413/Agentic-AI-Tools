import asyncio

class EventBroadcaster:
    def __init__(self):
        self.queue = asyncio.Queue()

    async def publish(self, message: str):
        
        await self.queue.put(message)

    async def listen(self):
      
        while True:
            msg = await self.queue.get()
            yield f"data: {msg}\n\n"

broadcaster = EventBroadcaster()
