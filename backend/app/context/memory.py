import json
import uuid

import httpx
from fastapi import HTTPException
from sqlalchemy import text

from app.context.retrieval import audit, namespace
from app.providers.dashscope import DashScopeClient, ProviderError
from app.security.guards import safe_output


class MemoryService:
    def __init__(self, settings, engine):
        self.settings, self.engine = settings, engine

    async def preference(self, actor, body):
        if actor.role != "customer":
            raise HTTPException(403, "customer_memory_role_required")
        async with self.engine.begin() as conn:
            key = str(uuid.uuid4())
            row = (
                await conn.execute(
                    text(
                        "INSERT INTO customer_memories(id,tenant_id,customer_id,kind,memory_key,content,validation,expires_at) "
                        "VALUES(:id,:tenant,:owner,'preference',:key,CAST(:content AS jsonb),'USER_CONFIRMED',"
                        "now()+make_interval(days=>:days)) ON CONFLICT(tenant_id,customer_id,kind,memory_key) "
                        "DO UPDATE SET content=EXCLUDED.content,expires_at=EXCLUDED.expires_at,deleted_at=NULL,"
                        "updated_at=now(),embedding=NULL RETURNING id"
                    ),
                    {
                        **namespace(actor),
                        "id": key,
                        "key": body.key,
                        "days": body.retain_days,
                        "content": json.dumps({"key": body.key, "value": body.value}),
                    },
                )
            ).scalar_one()
            await audit(conn, actor, row, "memory.user_confirmed")
        return {"id": str(row), "validation": "USER_CONFIRMED"}

    async def list(self, actor):
        if actor.role != "customer":
            raise HTTPException(403, "customer_memory_role_required")
        async with self.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id,kind,content,source_run_id,validation,generator_version,created_at,expires_at "
                            "FROM customer_memories WHERE tenant_id=:tenant AND customer_id=:owner "
                            "AND deleted_at IS NULL AND expires_at>now() ORDER BY updated_at DESC LIMIT 100"
                        ),
                        namespace(actor),
                    )
                )
                .mappings()
                .all()
            )
            enabled = await conn.scalar(
                text("SELECT memory_enabled FROM customer_accounts WHERE id=:owner"),
                namespace(actor),
            )
        return {
            "items": [safe_output(dict(row)) for row in rows],
            "automatic_memory_enabled": enabled,
        }

    async def delete(self, actor, key=None):
        if actor.role != "customer":
            raise HTTPException(403, "customer_memory_role_required")
        async with self.engine.begin() as conn:
            # Serialize deletion with background episode creation for the same customer.
            await conn.execute(
                text("SELECT id FROM customer_accounts WHERE id=:owner FOR UPDATE"),
                namespace(actor),
            )
            rows = (
                (
                    await conn.execute(
                        text(
                            "UPDATE customer_memories SET deleted_at=now(),content='{}',embedding=NULL,updated_at=now() "
                            "WHERE tenant_id=:tenant AND customer_id=:owner AND deleted_at IS NULL "
                            + ("AND id=:id " if key else "")
                            + "RETURNING id"
                        ),
                        {**namespace(actor), "id": str(key) if key else None},
                    )
                )
                .scalars()
                .all()
            )
            if key and not rows:
                raise HTTPException(404, "memory_not_found")
            if not key:
                await conn.execute(
                    text("UPDATE customer_accounts SET memory_enabled=false WHERE id=:owner"),
                    namespace(actor),
                )
            for resource in rows:
                await audit(conn, actor, resource, "memory.delete")
            enabled = await conn.scalar(
                text("SELECT memory_enabled FROM customer_accounts WHERE id=:owner"),
                namespace(actor),
            )
        return {"deleted": len(rows), "automatic_memory_enabled": enabled}

    async def settings_update(self, actor, enabled):
        if actor.role != "customer":
            raise HTTPException(403, "customer_memory_role_required")
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE customer_accounts SET memory_enabled=:enabled WHERE id=:owner AND tenant_id=:tenant"
                ),
                {**namespace(actor), "enabled": enabled},
            )
        return {"automatic_memory_enabled": enabled}

    async def retrieve(self, actor, query):
        if actor.role != "customer":
            return []
        base = (
            "tenant_id=:tenant AND customer_id=:owner AND deleted_at IS NULL AND expires_at>now()"
        )
        async with self.engine.connect() as conn:
            preferences = (
                (
                    await conn.execute(
                        text(
                            f"SELECT id,content FROM customer_memories WHERE {base} AND kind='preference' ORDER BY updated_at DESC LIMIT 3"
                        ),
                        namespace(actor),
                    )
                )
                .mappings()
                .all()
            )
            count = await conn.scalar(
                text(
                    f"SELECT count(*) FROM customer_memories WHERE {base} AND kind='episodic' AND embedding_model=:model"
                ),
                {**namespace(actor), "model": self.settings.embedding_model},
            )
        episodes = []
        if count:
            try:
                async with httpx.AsyncClient(
                    timeout=self.settings.provider_timeout_seconds
                ) as http:
                    vector = (await DashScopeClient(self.settings, http).embed([query]))[0]
                async with self.engine.connect() as conn:
                    episodes = (
                        (
                            await conn.execute(
                                text(
                                    f"SELECT id,content,source_run_id FROM customer_memories WHERE {base} AND kind='episodic' "
                                    "AND embedding IS NOT NULL AND embedding_model=:model "
                                    "ORDER BY embedding <=> CAST(:vector AS vector) LIMIT 3"
                                ),
                                {
                                    **namespace(actor),
                                    "model": self.settings.embedding_model,
                                    "vector": json.dumps(vector),
                                },
                            )
                        )
                        .mappings()
                        .all()
                    )
            except ProviderError:
                # Historical memories are optional; a provider outage cannot replace current facts.
                pass
        return [
            {
                "memory_id": str(row["id"]),
                "trust_class": "confirmed_preference",
                "content": row["content"],
            }
            for row in preferences
        ] + [
            {
                "memory_id": str(row["id"]),
                "trust_class": "historical_episode",
                "content": row["content"],
                "source_ref": "run:" + str(row["source_run_id"]),
            }
            for row in episodes
        ]

    async def fetch(self, actor, ids):
        if not ids or actor.role != "customer":
            return []
        async with self.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id,kind,content FROM customer_memories WHERE id=ANY(CAST(:ids AS uuid[])) "
                            "AND tenant_id=:tenant AND customer_id=:owner AND deleted_at IS NULL AND expires_at>now()"
                        ),
                        {**namespace(actor), "ids": ids},
                    )
                )
                .mappings()
                .all()
            )
        return [
            {
                "memory_id": str(row["id"]),
                "trust_class": "confirmed_preference"
                if row["kind"] == "preference"
                else "historical_episode",
                "content": row["content"],
            }
            for row in rows
        ]
