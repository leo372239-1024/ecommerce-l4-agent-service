import asyncio
import socket

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings


async def check():
    client = Redis.from_url(get_settings().redis_url, socket_timeout=2)
    try:
        alive = await client.exists(f"infra:worker:{socket.gethostname()}")
        return 0 if alive else 1
    except (RedisError, TimeoutError):
        return 1
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(check()))
