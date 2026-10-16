"""At-least-once background jobs with lease tokens; deleted/replaced resources cannot revive."""

import asyncio
import json
import uuid

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.business.catalog import CATALOG
from app.context.contracts import chunk_text, fingerprint
from app.context.retrieval import INDEX
from app.providers.dashscope import DashScopeClient, ProviderError

logger = structlog.get_logger()


class ContextJobs:
    def __init__(self, settings, engine):
        self.settings, self.engine = settings, engine

    async def claim(self, table, identity, where):
        # Identifiers/clauses are internal constants, never supplied by users or models.
        token = str(uuid.uuid4())
        async with self.engine.begin() as conn:
            return (
                (
                    await conn.execute(
                        text(
                            f"UPDATE {table} SET lease_token=:token,lease_until=now()+interval '120 seconds',"
                            f"attempts=attempts+1 WHERE {identity}=(SELECT {identity} FROM {table} WHERE {where} "
                            "AND next_attempt_at<=now() AND (lease_until IS NULL OR lease_until<now()) "
                            f"ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *"
                        ),
                        {"token": token},
                    )
                )
                .mappings()
                .first()
            )

    async def ensure_index(self, http):
        response = await http.get(self.settings.elasticsearch_url + "/" + INDEX)
        if response.status_code == 404:
            response = await http.put(
                self.settings.elasticsearch_url + "/" + INDEX,
                json={
                    "settings": {
                        "analysis": {
                            "analyzer": {
                                "zh_bigrams": {
                                    "tokenizer": "standard",
                                    "filter": ["lowercase", "cjk_bigram"],
                                }
                            }
                        }
                    },
                    "mappings": {
                        "properties": {
                            "document_id": {"type": "keyword"},
                            "version": {"type": "integer"},
                            "content": {"type": "text", "analyzer": "zh_bigrams"},
                        }
                    },
                },
            )
            if (
                response.status_code == 400
                and response.json().get("error", {}).get("type")
                == "resource_already_exists_exception"
            ):
                return
        response.raise_for_status()

    async def document(self):
        row = await self.claim("knowledge_documents", "id", "status IN ('QUEUED','INDEXING')")
        if not row:
            return False
        params = {"id": str(row["id"]), "token": str(row["lease_token"]), "version": row["version"]}
        guard = (
            "id=:id AND lease_token=:token AND version=:version AND status IN ('QUEUED','INDEXING')"
        )
        try:
            if self.settings.embedding_dimensions != 1024:
                raise ProviderError("unsupported_embedding_dimensions")
            parts = chunk_text(row["source_text"])
            async with httpx.AsyncClient(timeout=self.settings.provider_timeout_seconds) as http:
                provider = DashScopeClient(self.settings, http)
                if self.settings.elasticsearch_enabled:
                    await self.ensure_index(http)
                for offset in range(0, len(parts), 10):
                    async with self.engine.begin() as conn:
                        current = await conn.scalar(
                            text(
                                f"UPDATE knowledge_documents SET status='INDEXING',lease_until=now()+interval '120 seconds' WHERE {guard} RETURNING id"
                            ),
                            params,
                        )
                        if not current:
                            return True
                        existing = {
                            r["ordinal"]: dict(r)
                            for r in (
                                await conn.execute(
                                    text(
                                        "SELECT id,ordinal,content,content_hash FROM knowledge_chunks WHERE document_id=:id AND ordinal>=:offset AND ordinal<:end"
                                    ),
                                    {**params, "offset": offset, "end": offset + 10},
                                )
                            ).mappings()
                        }
                    batch = parts[offset : offset + 10]
                    missing = [
                        i
                        for i, part in enumerate(batch, offset)
                        if i not in existing or existing[i]["content_hash"] != fingerprint(part)
                    ]
                    vectors = await provider.embed([parts[i] for i in missing]) if missing else []
                    async with self.engine.begin() as conn:
                        current = await conn.scalar(
                            text(f"SELECT id FROM knowledge_documents WHERE {guard} FOR UPDATE"),
                            params,
                        )
                        if not current:
                            return True
                        for ordinal, vector in zip(missing, vectors, strict=True):
                            key = str(uuid.uuid5(row["id"], f"{row['version']}:{ordinal}"))
                            await conn.execute(
                                text(
                                    "INSERT INTO knowledge_chunks(id,document_id,ordinal,content,content_hash,embedding,embedding_model) "
                                    "VALUES(:chunk,:id,:ordinal,:content,:hash,CAST(:vector AS vector),:model) "
                                    "ON CONFLICT(document_id,ordinal) DO UPDATE SET id=EXCLUDED.id,content=EXCLUDED.content,"
                                    "content_hash=EXCLUDED.content_hash,embedding=EXCLUDED.embedding,embedding_model=EXCLUDED.embedding_model"
                                ),
                                {
                                    **params,
                                    "chunk": key,
                                    "ordinal": ordinal,
                                    "content": parts[ordinal],
                                    "hash": fingerprint(parts[ordinal]),
                                    "vector": json.dumps(vector),
                                    "model": self.settings.embedding_model,
                                },
                            )
                    if self.settings.elasticsearch_enabled:
                        lines = []
                        for ordinal, part in enumerate(batch, offset):
                            key = str(uuid.uuid5(row["id"], f"{row['version']}:{ordinal}"))
                            lines.extend(
                                [
                                    json.dumps({"index": {"_id": key}}),
                                    json.dumps(
                                        {
                                            "document_id": str(row["id"]),
                                            "version": row["version"],
                                            "content": part,
                                        },
                                        ensure_ascii=False,
                                    ),
                                ]
                            )
                        response = await http.post(
                            self.settings.elasticsearch_url + f"/{INDEX}/_bulk",
                            content="\n".join(lines) + "\n",
                            headers={"Content-Type": "application/x-ndjson"},
                        )
                        response.raise_for_status()
                        if response.json().get("errors"):
                            raise ProviderError("knowledge_index_bulk_failed")
                if self.settings.elasticsearch_enabled:
                    (
                        await http.post(self.settings.elasticsearch_url + f"/{INDEX}/_refresh")
                    ).raise_for_status()
            async with self.engine.begin() as conn:
                await conn.execute(
                    text(
                        f"UPDATE knowledge_documents SET status='READY',error_code=NULL,lexical_ready=:lexical,lease_token=NULL,lease_until=NULL,updated_at=now() WHERE {guard}"
                    ),
                    {**params, "lexical": self.settings.elasticsearch_enabled},
                )
        except (ProviderError, httpx.HTTPError, ValueError, SQLAlchemyError) as exc:
            async with self.engine.begin() as conn:
                await conn.execute(
                    text(
                        f"UPDATE knowledge_documents SET status=:status,error_code=:code,lease_token=NULL,lease_until=NULL,next_attempt_at=now()+interval '15 seconds' WHERE {guard}"
                    ),
                    {
                        **params,
                        "status": "FAILED" if row["attempts"] >= 3 else "QUEUED",
                        "code": "ingestion_" + type(exc).__name__,
                    },
                )
        return True

    async def cleanup(self):
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE customer_memories SET content='{}',embedding=NULL,deleted_at=now() WHERE expires_at<=now() AND deleted_at IS NULL"
                )
            )
            deleted = (
                (
                    await conn.execute(
                        text(
                            "SELECT id FROM knowledge_documents WHERE status='DELETED' AND NOT lexical_ready LIMIT 10"
                        )
                    )
                )
                .scalars()
                .all()
            )
        if deleted and self.settings.elasticsearch_enabled:
            async with httpx.AsyncClient(timeout=10) as http:
                response = await http.post(
                    self.settings.elasticsearch_url + f"/{INDEX}/_delete_by_query?refresh=true",
                    json={"query": {"terms": {"document_id": [str(key) for key in deleted]}}},
                )
                if response.status_code != 404:
                    response.raise_for_status()
                async with self.engine.begin() as conn:
                    await conn.execute(
                        text(
                            "UPDATE knowledge_documents SET lexical_ready=true WHERE id=ANY(CAST(:ids AS uuid[])) AND status='DELETED' "
                            "AND updated_at<now()-interval '120 seconds'"
                        ),
                        {"ids": [str(key) for key in deleted]},
                    )
        # An old publisher can finish its HTTP call after replacement. Repeated cleanup over
        # the lease window removes such late old-version writes; PG excludes them immediately.
        if self.settings.elasticsearch_enabled:
            async with self.engine.connect() as conn:
                replaced = (
                    (
                        await conn.execute(
                            text(
                                "SELECT id,version FROM knowledge_documents WHERE version>1 AND status='READY' "
                                "AND updated_at>now()-interval '120 seconds' LIMIT 10"
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            if replaced:
                async with httpx.AsyncClient(timeout=10) as http:
                    for row in replaced:
                        response = await http.post(
                            self.settings.elasticsearch_url
                            + f"/{INDEX}/_delete_by_query?refresh=true",
                            json={
                                "query": {
                                    "bool": {
                                        "filter": [
                                            {"term": {"document_id": str(row["id"])}},
                                            {"range": {"version": {"lt": row["version"]}}},
                                        ]
                                    }
                                }
                            },
                        )
                        if response.status_code != 404:
                            response.raise_for_status()

    async def episode(self):
        row = await self.claim("memory_outbox", "workflow_id", "status='PENDING'")
        if not row:
            return False
        params = {"id": str(row["workflow_id"]), "token": str(row["lease_token"])}
        try:
            async with self.engine.connect() as conn:
                run = (
                    (
                        await conn.execute(
                            text(
                                "SELECT r.*,a.tenant_id,a.memory_enabled FROM agent_runs r JOIN customer_accounts a ON a.id=r.customer_id WHERE r.id=:id"
                            ),
                            params,
                        )
                    )
                    .mappings()
                    .one()
                )
                spec = await conn.scalar(
                    text(
                        "SELECT payload->'value' FROM agent_events WHERE workflow_id=:id AND payload->>'name' IN ('business.task_spec','business.task_spec.resolved') ORDER BY seq DESC LIMIT 1"
                    ),
                    params,
                )
                tools = (
                    (
                        await conn.execute(
                            text(
                                "SELECT DISTINCT payload->'value'->>'selected_tool' FROM agent_events WHERE workflow_id=:id AND payload->>'name'='business.tool_route'"
                            ),
                            params,
                        )
                    )
                    .scalars()
                    .all()
                )
            allowed_goals = {
                "order",
                "logistics",
                "eligibility",
                "refund_quote",
                "business_query",
                "business_write",
                "submit_refund",
                "create_return",
                "cancel_order",
                "unsupported",
                "knowledge",
            }
            content = {
                "goals": sorted(g for g in (spec or {}).get("goals", []) if g in allowed_goals),
                "status": run["status"],
                "tools": sorted(t for t in tools if t in CATALOG),
                "has_unresolved_steps": bool((run["result"] or {}).get("failures")),
                "historical_only": True,
            }
            async with httpx.AsyncClient(timeout=self.settings.provider_timeout_seconds) as http:
                vector = (
                    (await DashScopeClient(self.settings, http).embed([json.dumps(content)]))[0]
                    if run["memory_enabled"]
                    else None
                )
            async with self.engine.begin() as conn:
                enabled = await conn.scalar(
                    text("SELECT memory_enabled FROM customer_accounts WHERE id=:owner FOR UPDATE"),
                    {"owner": run["customer_id"]},
                )
                claimed = await conn.scalar(
                    text(
                        "SELECT workflow_id FROM memory_outbox WHERE workflow_id=:id AND lease_token=:token FOR UPDATE"
                    ),
                    params,
                )
                if not claimed:
                    return True
                if enabled and vector:
                    await conn.execute(
                        text(
                            "INSERT INTO customer_memories(id,tenant_id,customer_id,kind,memory_key,content,source_run_id,validation,embedding,embedding_model,expires_at) "
                            "VALUES(:memory,:tenant,:owner,'episodic',:memory_key,CAST(:content AS jsonb),:id,'VERIFIED_TRACE',CAST(:vector AS vector),:model,now()+interval '90 days') ON CONFLICT(tenant_id,customer_id,kind,memory_key) DO NOTHING"
                        ),
                        {
                            **params,
                            "memory": str(uuid.uuid4()),
                            "memory_key": str(row["workflow_id"]),
                            "tenant": run["tenant_id"],
                            "owner": run["customer_id"],
                            "content": json.dumps(content),
                            "vector": json.dumps(vector),
                            "model": self.settings.embedding_model,
                        },
                    )
                await conn.execute(
                    text(
                        "UPDATE memory_outbox SET status='DONE',lease_token=NULL,lease_until=NULL,error_code=NULL WHERE workflow_id=:id AND lease_token=:token"
                    ),
                    params,
                )
        except (ProviderError, httpx.HTTPError, ValueError, SQLAlchemyError) as exc:
            async with self.engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE memory_outbox SET status=:status,lease_token=NULL,lease_until=NULL,error_code=:code,next_attempt_at=now()+interval '15 seconds' WHERE workflow_id=:id AND lease_token=:token"
                    ),
                    {
                        **params,
                        "status": "FAILED" if row["attempts"] >= 3 else "PENDING",
                        "code": "memory_" + type(exc).__name__,
                    },
                )
        return True

    async def run(self, stop):
        while not stop.is_set():
            try:
                worked = await self.document()
                await self.episode()
                await self.cleanup()
            except (SQLAlchemyError, httpx.HTTPError, OSError, ValueError, ProviderError) as exc:
                logger.warning("context_job_dependency_unavailable", error_type=type(exc).__name__)
                worked = False
            try:
                await asyncio.wait_for(stop.wait(), 0.5 if worked else 2)
            except TimeoutError:
                pass
