"""Redis hints reduce polling latency. PostgreSQL remains the durable source of truth."""

import asyncio
from collections import defaultdict

from redis.exceptions import RedisError


class EventWakeups:
    def __init__(self, redis):
        self.redis = redis
        self.waiters = defaultdict(set)
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.listen())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def wait(self, workflow_id=None, timeout=1):
        event = asyncio.Event()
        self.waiters[workflow_id].add(event)
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            pass
        finally:
            self.waiters[workflow_id].discard(event)
            if not self.waiters[workflow_id]:
                del self.waiters[workflow_id]

    async def listen(self):
        while True:
            try:
                async with self.redis.pubsub() as subscription:
                    await subscription.subscribe("agent:events")
                    while True:
                        message = await subscription.get_message(
                            ignore_subscribe_messages=True, timeout=1
                        )
                        if message is None:
                            continue
                        if message["type"] != "message":
                            continue
                        value = message["data"]
                        if isinstance(value, bytes):
                            value = value.decode("ascii", errors="ignore")
                        workflow_id = value.split(":", 1)[0]
                        for key in (workflow_id, None):
                            for waiter in self.waiters.get(key, ()):
                                waiter.set()
            except (RedisError, OSError, TimeoutError):
                # Lost/duplicate notifications cannot lose work: callers still poll PostgreSQL.
                await asyncio.sleep(1)
