import asyncio

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings

SCHEMA_REVISION = "0010_conversations"


class Resources:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine = create_async_engine(
            settings.database_url,
            pool_size=settings.database_pool_size,
            max_overflow=0,
            pool_pre_ping=True,
            pool_timeout=settings.dependency_timeout_seconds,
            connect_args={"timeout": settings.dependency_timeout_seconds, "command_timeout": 5},
        )
        self.redis = Redis.from_url(
            settings.redis_url,
            socket_timeout=settings.dependency_timeout_seconds,
            socket_connect_timeout=settings.dependency_timeout_seconds,
            max_connections=20,
        )
        self.http = httpx.AsyncClient(timeout=settings.dependency_timeout_seconds)

    async def _postgres(self) -> None:
        async with self.engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != SCHEMA_REVISION:
                raise RuntimeError("schema revision mismatch")

    async def _elasticsearch(self) -> None:
        response = await self.http.get(f"{self.settings.elasticsearch_url}/_cluster/health")
        response.raise_for_status()
        if response.json().get("status") not in {"green", "yellow"}:
            raise RuntimeError("search unavailable")

    async def _sandbox(self) -> None:
        from fastapi import HTTPException

        from app.security.sandbox import invoke

        try:
            value = await invoke(
                self.settings.sandbox_socket_path,
                {"tool": "money_total", "arguments": {"amounts": ["0.10", "0.20"]}},
            )
            if value.get("total") != "0.30":
                raise RuntimeError("sandbox contract unavailable")
        except HTTPException:
            raise RuntimeError("sandbox unavailable") from None

    async def checks(self) -> dict[str, str]:
        async def probe(awaitable) -> str:
            try:
                await asyncio.wait_for(awaitable, self.settings.dependency_timeout_seconds)
                return "ok"
            except (
                SQLAlchemyError,
                RedisError,
                httpx.HTTPError,
                TimeoutError,
                RuntimeError,
                ValueError,
                OSError,
            ):
                # Exception text can contain credentials. Return only a safe status.
                return "unavailable"

        checks = [self._postgres(), self.redis.ping()]
        names = ["postgres_schema", "redis"]
        if self.settings.elasticsearch_enabled:
            checks.append(self._elasticsearch())
            names.append("elasticsearch")
        if self.settings.sandbox_enabled:
            checks.append(self._sandbox())
            names.append("sandbox")
        results = await asyncio.gather(*(probe(check) for check in checks))
        return dict(zip(names, results, strict=True))

    async def close(self) -> None:
        await self.http.aclose()
        await self.redis.aclose()
        await self.engine.dispose()
