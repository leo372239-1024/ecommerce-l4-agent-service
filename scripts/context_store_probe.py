"""Isolated real-PG fault regressions with deterministic embedding adapters (no paid API)."""

import asyncio
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from app.context.contracts import DocumentInput, PreferenceInput
from app.context.jobs import ContextJobs
from app.context.memory import MemoryService
from app.context.retrieval import KnowledgeService
from app.core.auth import Principal
from app.core.config import get_settings
from app.core.resources import Resources
from app.providers.dashscope import DashScopeClient, ProviderError


async def main():
    settings = get_settings().model_copy(update={"elasticsearch_enabled": False})
    resources = Resources(settings)
    engine = resources.engine
    token = uuid.uuid4().hex
    actor = Principal("context-probe-" + token, "customer", "context-probe-a-" + token)
    other = Principal("context-probe-other-" + token, "customer", "context-probe-b-" + token)
    checks = []
    service, memory = KnowledgeService(settings, engine), MemoryService(settings, engine)
    original = DashScopeClient.embed

    class ProbeJobs(ContextJobs):
        async def claim(self, table, identity, where):
            # The only dispatchable fixtures are in this probe's unique namespace.
            restriction = (
                f" AND tenant_id='{actor.tenant_id}'"
                if table == "knowledge_documents"
                else f" AND workflow_id IN (SELECT id FROM agent_runs WHERE customer_id='{actor.id}')"
            )
            return await super().claim(table, identity, where + restriction)

    jobs = ProbeJobs(settings, engine)

    async def embed(_self, texts):
        return [[1.0, *([0.0] * 1023)] for _ in texts]

    async def row(key):
        async with engine.connect() as conn:
            return dict(
                (
                    await conn.execute(
                        text("SELECT * FROM knowledge_documents WHERE id=:id"), {"id": key}
                    )
                )
                .mappings()
                .one()
            )

    async def count(key):
        async with engine.connect() as conn:
            return await conn.scalar(
                text("SELECT count(*) FROM knowledge_chunks WHERE document_id=:id"), {"id": key}
            )

    try:
        async with engine.begin() as conn:
            for person in (actor, other):
                await conn.execute(
                    text(
                        "INSERT INTO customer_accounts(id,username,password_hash,tenant_id) VALUES(:id,:id,'disabled-probe',:tenant)"
                    ),
                    {"id": person.id, "tenant": person.tenant_id},
                )
        DashScopeClient.embed = embed
        document = await service.create(actor, DocumentInput(title="probe", text="原始资料" * 350))
        key = document["id"]
        claimed = await asyncio.gather(jobs.document(), jobs.document())
        assert sorted(claimed) == [False, True]
        assert (await row(key))["status"] == "READY" and await count(key) == 3
        checks.append("concurrent_ingestion_claim_is_unique")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE knowledge_documents SET status='INDEXING',lease_until=now()-interval '1 second',lease_token=:token,next_attempt_at=now() WHERE id=:id"
                ),
                {"id": key, "token": str(uuid.uuid4())},
            )
        assert await jobs.document()
        assert await count(key) == 3 and (await row(key))["status"] == "READY"
        checks.append("expired_lease_recovery_keeps_unique_chunks")

        async with engine.connect() as conn:
            chunk = await conn.scalar(
                text("SELECT id FROM knowledge_chunks WHERE document_id=:id LIMIT 1"), {"id": key}
            )
        assert await service.fetch(other, [str(chunk)]) == []
        assert (await service.search(other, "原始资料"))["diagnostics"]["authorized_chunks"] == 0
        # Even the correct customer ID cannot cross a tenant boundary.
        assert (
            await service.fetch(Principal(actor.id, "customer", other.tenant_id), [str(chunk)])
            == []
        )
        checks.append("tenant_and_customer_filter_precedes_provider")

        pending = await service.create(
            actor, DocumentInput(title="delete-race", text="删除中的资料")
        )
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(_self, texts):
            entered.set()
            await release.wait()
            return await embed(_self, texts)

        DashScopeClient.embed = delayed
        task = asyncio.create_task(jobs.document())
        await asyncio.wait_for(entered.wait(), 10)
        await service.mutate(actor, pending["id"], "delete", 1)
        release.set()
        await task
        assert (await row(pending["id"]))["status"] == "DELETED" and await count(pending["id"]) == 0
        checks.append("delete_during_embedding_cannot_resurrect_document")

        replacement = await service.create(
            actor, DocumentInput(title="replace-race", text="旧版本资料")
        )
        entered.clear()
        release.clear()
        task = asyncio.create_task(jobs.document())
        await asyncio.wait_for(entered.wait(), 10)
        await service.mutate(
            actor,
            replacement["id"],
            "replace",
            1,
            DocumentInput(title="replace-race", text="新版本资料"),
        )
        release.set()
        await task
        assert await count(replacement["id"]) == 0
        DashScopeClient.embed = embed
        await jobs.document()
        assert (await row(replacement["id"]))["version"] == 2 and await count(
            replacement["id"]
        ) == 1
        checks.append("stale_ingestion_lease_cannot_publish_replacement")

        failure = await service.create(actor, DocumentInput(title="retry", text="暂态故障资料"))

        async def unavailable(_self, texts):
            raise ProviderError("synthetic_outage")

        DashScopeClient.embed = unavailable
        for _ in range(3):
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE knowledge_documents SET next_attempt_at=now() WHERE id=:id"),
                    {"id": failure["id"]},
                )
            await jobs.document()
        assert (await row(failure["id"]))["status"] == "FAILED"
        await service.mutate(actor, failure["id"], "retry", 1)
        DashScopeClient.embed = embed
        await jobs.document()
        assert (await row(failure["id"]))["status"] == "READY"
        checks.append("bounded_provider_failure_and_explicit_retry")

        preference = await memory.preference(
            actor, PreferenceInput(key="response_style", value="concise")
        )
        assert await memory.fetch(other, [preference["id"]]) == []
        enabled_run = str(uuid.uuid4())
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO agent_runs(id,customer_id,message,request_key,request_hash,status,invocation_id,result) VALUES(:id,:owner,'probe','enabled-probe','probe','SUCCEEDED',:id,'{}')"
                ),
                {"id": enabled_run, "owner": actor.id},
            )
            await conn.execute(
                text("INSERT INTO memory_outbox(workflow_id) VALUES(:id)"), {"id": enabled_run}
            )
        await jobs.episode()
        episodes = [
            item
            for item in (await memory.list(actor))["items"]
            if str(item["source_run_id"]) == enabled_run
        ]
        assert len(episodes) == 1 and episodes[0]["content"]["historical_only"]
        checks.append("enabled_episode_persists_vector_and_source_reference")
        await memory.delete(actor)
        assert await memory.fetch(actor, [preference["id"]]) == []
        workflow_id = str(uuid.uuid4())
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO agent_runs(id,customer_id,message,request_key,request_hash,status,invocation_id,result) VALUES(:id,:owner,'probe','probe','probe','SUCCEEDED',:id,'{}')"
                ),
                {"id": workflow_id, "owner": actor.id},
            )
            await conn.execute(
                text("INSERT INTO memory_outbox(workflow_id) VALUES(:id)"), {"id": workflow_id}
            )
        DashScopeClient.embed = unavailable
        await jobs.episode()
        assert (await memory.list(actor))["items"] == []
        async with engine.connect() as conn:
            assert (
                await conn.scalar(
                    text("SELECT status FROM memory_outbox WHERE workflow_id=:id"),
                    {"id": workflow_id},
                )
                == "DONE"
            )
        checks.append("forget_all_disables_late_episode_creation")
        return {
            "captured_at": datetime.now(UTC).isoformat(),
            "checks": checks,
            "status": "passed",
            "embedding_adapter": "deterministic_stub",
            "storage": "real_postgresql",
        }
    finally:
        DashScopeClient.embed = original
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM context_audit WHERE tenant_id=ANY(:tenants)"),
                {"tenants": [actor.tenant_id, other.tenant_id]},
            )
            await conn.execute(
                text("DELETE FROM customer_memories WHERE customer_id=ANY(:owners)"),
                {"owners": [actor.id, other.id]},
            )
            await conn.execute(
                text(
                    "DELETE FROM memory_outbox WHERE workflow_id IN (SELECT id FROM agent_runs WHERE customer_id=:owner)"
                ),
                {"owner": actor.id},
            )
            await conn.execute(
                text("DELETE FROM agent_runs WHERE customer_id=:owner"), {"owner": actor.id}
            )
            await conn.execute(
                text("DELETE FROM knowledge_documents WHERE tenant_id=ANY(:tenants)"),
                {"tenants": [actor.tenant_id, other.tenant_id]},
            )
            await conn.execute(
                text("DELETE FROM customer_accounts WHERE id=ANY(:owners)"),
                {"owners": [actor.id, other.id]},
            )
        await resources.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main())))
