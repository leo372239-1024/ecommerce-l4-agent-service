"""Authorization precedes both retrievers; PostgreSQL fences stale Elasticsearch hits."""

import asyncio
import json
import uuid
from datetime import UTC, datetime

import httpx
from fastapi import HTTPException
from sqlalchemy import text

from app.business.routing import rrf
from app.context.contracts import chunk_text, fingerprint
from app.providers.dashscope import DashScopeClient, ProviderError
from app.security.guards import redact_text, safe_output, signals, valid_text

INDEX = "l4-knowledge-v1"

# No client/model namespace is accepted. The actor is resolved from the shared session.
VISIBLE = """d.tenant_id=:tenant AND (d.scope='tenant' OR d.owner_id=:owner)
 AND EXISTS(SELECT 1 FROM customer_accounts a WHERE a.id=:owner AND a.tenant_id=:tenant AND a.role=:role AND a.active)
 AND :role=ANY(d.visible_roles) AND d.status='READY'
 AND d.effective_at<=now() AND (d.expires_at IS NULL OR d.expires_at>now())
 AND (d.kind!='policy' OR d.validation='APPROVED')"""


def namespace(actor):
    return {"tenant": actor.tenant_id, "owner": actor.id, "role": actor.role}


async def audit(conn, actor, resource, action):
    await conn.execute(
        text(
            "INSERT INTO context_audit(tenant_id,actor_id,resource_id,action) VALUES(:tenant,:owner,:id,:action)"
        ),
        {**namespace(actor), "id": str(resource), "action": action},
    )


class KnowledgeService:
    def __init__(self, settings, engine):
        self.settings, self.engine = settings, engine

    async def create(self, actor, body, source_name="api-text"):
        try:
            source_name = redact_text(valid_text(source_name))
        except ValueError:
            raise HTTPException(422, "invalid_source_name") from None
        if body.scope == "tenant" and actor.role not in {"support", "supervisor"}:
            raise HTTPException(403, "tenant_publisher_role_required")
        if body.kind == "policy" and actor.role != "supervisor":
            raise HTTPException(403, "policy_publisher_role_required")
        chunk_text(body.text)
        if self.settings.embedding_dimensions != 1024:
            raise HTTPException(503, "knowledge_requires_1024_dimensions")
        key = str(uuid.uuid4())
        async with self.engine.begin() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:owner,0))"),
                {"owner": "knowledge-admission:" + actor.id},
            )
            pending = await conn.scalar(
                text(
                    "SELECT count(*) FROM knowledge_documents WHERE owner_id=:owner AND status IN ('QUEUED','INDEXING')"
                ),
                namespace(actor),
            )
            if pending >= 5:
                raise HTTPException(429, "knowledge_pending_limit")
            await conn.execute(
                text(
                    "INSERT INTO knowledge_documents(id,tenant_id,owner_id,scope,title,kind,source_text,source_name,"
                    "content_hash,visible_roles,effective_at,expires_at) VALUES(:id,:tenant,:owner,:scope,:title,"
                    ":kind,:text,:source,:hash,:roles,COALESCE(:effective,now()),:expires)"
                ),
                {
                    **namespace(actor),
                    "id": key,
                    "scope": body.scope,
                    "title": body.title,
                    "kind": body.kind,
                    "text": body.text,
                    "source": source_name,
                    "hash": fingerprint(body.text),
                    "roles": body.visible_roles,
                    "effective": body.effective_at,
                    "expires": body.expires_at,
                },
            )
            await audit(conn, actor, key, "knowledge.create")
        return {"id": key, "status": "QUEUED", "version": 1}

    async def manage(self, conn, actor, key):
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT * FROM knowledge_documents WHERE id=:id AND tenant_id=:tenant "
                        "AND (owner_id=:owner OR (scope='tenant' AND :role='supervisor')) FOR UPDATE"
                    ),
                    {**namespace(actor), "id": str(key)},
                )
            )
            .mappings()
            .first()
        )
        if not row or row["status"] == "DELETED":
            raise HTTPException(404, "document_not_found")
        return dict(row)

    async def mutate(self, actor, key, action, expected_version, body=None):
        async with self.engine.begin() as conn:
            row = await self.manage(conn, actor, key)
            if row["version"] != expected_version:
                raise HTTPException(409, "document_version_conflict")
            params = {"id": str(key)}
            if action == "approve":
                if actor.role != "supervisor" or row["status"] != "READY":
                    raise HTTPException(403, "ready_document_supervisor_review_required")
                await conn.execute(
                    text(
                        "UPDATE knowledge_documents SET validation='APPROVED',reviewed_by=:reviewer,updated_at=now() WHERE id=:id"
                    ),
                    {**params, "reviewer": actor.id},
                )
            elif action == "delete":
                await conn.execute(
                    text(
                        "UPDATE knowledge_documents SET status='DELETED',source_text='',lease_token=NULL,"
                        "lease_until=NULL,updated_at=now() WHERE id=:id"
                    ),
                    params,
                )
                await conn.execute(
                    text("DELETE FROM knowledge_chunks WHERE document_id=:id"), params
                )
            elif action == "replace":
                if body.scope != row["scope"] or body.kind != row["kind"]:
                    raise HTTPException(422, "document_scope_and_kind_immutable")
                if row["kind"] == "policy" and actor.role != "supervisor":
                    raise HTTPException(403, "policy_publisher_role_required")
                chunk_text(body.text)
                await conn.execute(
                    text("DELETE FROM knowledge_chunks WHERE document_id=:id"), params
                )
                await conn.execute(
                    text(
                        "UPDATE knowledge_documents SET title=:title,source_text=:text,content_hash=:hash,version=version+1,"
                        "visible_roles=:roles,effective_at=COALESCE(:effective,now()),expires_at=:expires,"
                        "status='QUEUED',validation='UNREVIEWED',reviewed_by=NULL,attempts=0,error_code=NULL,"
                        "lease_token=NULL,lease_until=NULL,next_attempt_at=now(),lexical_ready=false,updated_at=now() WHERE id=:id"
                    ),
                    {
                        **params,
                        "title": body.title,
                        "text": body.text,
                        "hash": fingerprint(body.text),
                        "roles": body.visible_roles,
                        "effective": body.effective_at,
                        "expires": body.expires_at,
                    },
                )
            elif action == "retry":
                if row["status"] != "FAILED":
                    raise HTTPException(409, "failed_ingestion_required")
                await conn.execute(
                    text(
                        "UPDATE knowledge_documents SET status='QUEUED',attempts=0,error_code=NULL,next_attempt_at=now(),lease_token=NULL,lease_until=NULL WHERE id=:id"
                    ),
                    params,
                )
            else:
                raise ValueError("unknown_document_mutation")
            await audit(conn, actor, key, "knowledge." + action)
        return {
            "id": str(key),
            "action": action,
            "version": expected_version + (action == "replace"),
        }

    async def list(self, actor):
        async with self.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id,title,kind,scope,status,validation,version,content_hash,source_name,"
                            "created_at,effective_at,expires_at,error_code,lexical_ready,visible_roles,"
                            "(d.owner_id=:owner OR (d.scope='tenant' AND :role='supervisor')) AS can_manage "
                            "FROM knowledge_documents d "
                            "WHERE d.tenant_id=:tenant AND (d.owner_id=:owner OR (d.scope='tenant' AND :role=ANY(d.visible_roles))) "
                            "AND d.status!='DELETED' ORDER BY d.created_at DESC LIMIT 100"
                        ),
                        namespace(actor),
                    )
                )
                .mappings()
                .all()
            )
        return {"items": [safe_output(dict(row)) for row in rows]}

    async def search(self, actor, query, limit=5):
        started = datetime.now(UTC)
        async with self.engine.connect() as conn:
            allowed = (
                (
                    await conn.execute(
                        text(
                            "SELECT c.id FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id "
                            f"WHERE {VISIBLE} AND c.embedding_model=:model ORDER BY c.id LIMIT 10001"
                        ),
                        {**namespace(actor), "model": self.settings.embedding_model},
                    )
                )
                .scalars()
                .all()
            )
        if len(allowed) > 10000:
            raise HTTPException(503, "knowledge_namespace_capacity_exceeded")
        ids = [str(key) for key in allowed]
        info = {"authorized_chunks": len(ids), "retrieval": "empty", "reranker_degraded": False}
        if not ids:
            return {"items": [], "diagnostics": info}
        async with httpx.AsyncClient(timeout=self.settings.provider_timeout_seconds) as http:
            provider = DashScopeClient(self.settings, http)

            async def lexical():
                if not self.settings.elasticsearch_enabled:
                    raise ProviderError("lexical_search_disabled")
                response = await http.post(
                    self.settings.elasticsearch_url + f"/{INDEX}/_search",
                    json={
                        "size": 30,
                        "_source": False,
                        "query": {
                            "bool": {
                                "filter": [{"ids": {"values": ids}}],
                                "must": [{"match": {"content": query}}],
                            }
                        },
                    },
                )
                response.raise_for_status()
                return [hit["_id"] for hit in response.json()["hits"]["hits"] if hit["_id"] in ids]

            async def dense():
                vector = (await provider.embed([query]))[0]
                async with self.engine.connect() as conn:
                    return [
                        str(key)
                        for key in (
                            await conn.execute(
                                text(
                                    "SELECT id FROM knowledge_chunks WHERE id=ANY(CAST(:ids AS uuid[])) "
                                    "ORDER BY embedding <=> CAST(:vector AS vector) LIMIT 30"
                                ),
                                {"ids": ids, "vector": json.dumps(vector)},
                            )
                        ).scalars()
                    ]

            rankings = await asyncio.gather(lexical(), dense(), return_exceptions=True)
            valid = [ranking for ranking in rankings if isinstance(ranking, list)]
            if not valid:
                raise HTTPException(503, "knowledge_retrieval_unavailable")
            merged = rrf(valid, 20)
            info["retrieval"] = "hybrid_rrf" if len(valid) == 2 else "degraded_single_retriever"
            # Reauthorize after network calls, before text reaches the reranker or the caller.
            rows = await self.fetch(actor, merged)
            by_id = {str(row["id"]): row for row in rows}
            merged = [key for key in merged if key in by_id]
            if merged:
                try:
                    ranked = await provider.rerank(
                        query, [by_id[k]["content"] for k in merged], min(limit, len(merged))
                    )
                    keys = [merged[item.index] for item in ranked]
                except ProviderError:
                    keys, info["reranker_degraded"] = merged[:limit], True
            else:
                keys = []
            final = {str(row["id"]): row for row in await self.fetch(actor, keys)}
        items = [self.citation(final[key]) for key in keys if key in final]
        info["elapsed_ms"] = round((datetime.now(UTC) - started).total_seconds() * 1000, 2)
        return {"items": items, "diagnostics": info}

    async def fetch(self, actor, ids):
        if not ids:
            return []
        async with self.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT c.id,c.document_id,c.ordinal,c.content,c.content_hash,d.title,d.version,"
                            "d.kind,d.validation,d.source_name,d.effective_at,d.expires_at "
                            f"FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id WHERE {VISIBLE} "
                            "AND c.id=ANY(CAST(:ids AS uuid[])) AND c.embedding_model=:model"
                        ),
                        {**namespace(actor), "ids": ids, "model": self.settings.embedding_model},
                    )
                )
                .mappings()
                .all()
            )
        # Legacy records may predate ingress guards. Recheck before reranking, generation,
        # fallback excerpts or evidence export, never just in a model prompt.
        clean = []
        for row in rows:
            if signals(dict(row)):
                continue
            value = safe_output(dict(row))
            if value["content"] != row["content"]:
                value["content_hash"] = fingerprint(value["content"])
            clean.append(value)
        return clean

    @staticmethod
    def citation(row):
        return {
            **{
                key: row[key]
                for key in (
                    "ordinal",
                    "content",
                    "content_hash",
                    "title",
                    "version",
                    "kind",
                    "validation",
                    "source_name",
                )
            },
            "chunk_id": str(row["id"]),
            "document_id": str(row["document_id"]),
            "evidence_ref": "knowledge:" + str(row["id"]),
            "trust_class": "reviewed_reference"
            if row["validation"] == "APPROVED"
            else "untrusted_reference",
            "effective_at": row["effective_at"].isoformat(),
            "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
            "retrieved_at": datetime.now(UTC).isoformat(),
        }

    async def evidence(self, actor, key):
        rows = await self.fetch(actor, [str(key)])
        if not rows:
            raise HTTPException(404, "knowledge_evidence_not_found")
        return self.citation(rows[0])
