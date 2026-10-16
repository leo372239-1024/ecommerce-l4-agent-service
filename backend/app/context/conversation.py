"""Conversation references are user intent, never current facts or authorization."""

import re
import uuid

from fastapi import HTTPException
from sqlalchemy import text

from app.agent.contracts import TaskSpec
from app.security.guards import safe_output


def compact_turns(turns, recent=5):
    """Lossless entity ledger plus bounded recent dialogue; no generative fact summary."""
    entities, ledger = [], []
    for turn in turns:
        ids = list(dict.fromkeys(turn.get("order_ids", [])))
        # Preserve first textual mention rather than a model's arbitrary array ordering.
        message = turn.get("message", "")
        ids.sort(
            key=lambda identifier: (
                message.find(identifier) if identifier in message else len(message)
            )
        )
        entities.extend(identifier for identifier in ids if identifier not in entities)
        if ids:
            entry = {"seq": turn["seq"], "order_ids": ids, "source_run_id": turn["run_id"]}
            if ledger and ledger[-1]["order_ids"] == ids:
                ledger[-1] = entry
            else:
                ledger.append(entry)
    return {
        "trust_class": "historical_user_reference_only",
        "entities": entities,
        "entity_ledger": ledger,
        "recent_turns": [
            {
                **{k: t[k] for k in ("seq", "run_id", "status")},
                "message_excerpt": t["message"][:200],
            }
            for t in turns[-recent:]
        ],
        "history_count": len(turns),
        "facts_rule": "Re-read current merchant facts. No prior approvals, amounts or permissions.",
    }


def reference_ids(message, packet):
    """Resolve only explicit anaphora. Ambiguous references require user confirmation."""
    if not re.search(
        r"最开始|最早|第一次|第一个|第二个|第三个|上一个|上一笔|刚才|同一|那个|这个|这些|那两",
        message,
    ):
        return []
    entities = packet.get("entities", [])
    ordinal = [
        index
        for pattern, index in ((r"最开始|最早|第一次|第一个", 0), (r"第二个", 1), (r"第三个", 2))
        if re.search(pattern, message)
    ]
    if ordinal:
        if re.search(r"不要|不查|无需", message) and len(ordinal) > 1:
            return []
        if any(index >= len(entities) for index in ordinal):
            return []
        resolved = [entities[index] for index in ordinal]
        if re.search(r"上一个|上一笔|刚才|同一", message):
            ledger = packet.get("entity_ledger", [])
            if not ledger or len(ledger[-1]["order_ids"]) != 1:
                return []
            resolved += ledger[-1]["order_ids"]
        return list(dict.fromkeys(resolved)) if len(set(resolved)) <= 3 else []
    ledger = packet.get("entity_ledger", [])
    if not ledger:
        return []
    ids = ledger[-1]["order_ids"]
    if re.search(r"这些|那两", message):
        return ids if len(ids) <= 3 else []
    return ids if len(ids) == 1 else []


def model_context(message, packet):
    ids = reference_ids(message, packet)
    return {
        "trust_class": packet["trust_class"],
        "history_count": packet["history_count"],
        "reference_ids": ids,
        "reference_resolution": {
            "kind": "explicit_anaphora" if ids else "unresolved",
            "order_ids": ids,
            "authority": "identity_binding_only_not_business_facts_or_permissions",
        },
        "source_refs": [row for row in packet["entity_ledger"] if set(row["order_ids"]) & set(ids)][
            -3:
        ],
        "recent_turns": packet["recent_turns"],
        "facts_rule": packet["facts_rule"],
    }


class ConversationService:
    def __init__(self, engine):
        self.engine = engine

    async def create(self, owner):
        identifier = str(uuid.uuid4())
        async with self.engine.begin() as conn:
            # Bound retained conversations per account, independently of task admission.
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "conversation:" + owner},
            )
            count = await conn.scalar(
                text(
                    "SELECT count(*) FROM agent_conversations WHERE customer_id=:owner AND deleted_at IS NULL AND expires_at>now()"
                ),
                {"owner": owner},
            )
            if count >= 100:
                raise HTTPException(429, "conversation_limit")
            await conn.execute(
                text("INSERT INTO agent_conversations(id,customer_id) VALUES(:id,:owner)"),
                {"id": identifier, "owner": owner},
            )
        return {"id": identifier, "retention_days": 30}

    async def turns(self, owner, identifier, before_seq=None):
        async with self.engine.connect() as conn:
            exists = await conn.scalar(
                text(
                    "SELECT id FROM agent_conversations WHERE id=:id AND customer_id=:owner AND deleted_at IS NULL AND expires_at>now()"
                ),
                {"id": str(identifier), "owner": owner},
            )
            if not exists:
                raise HTTPException(404, "conversation_not_found")
            rows = (
                (
                    await conn.execute(
                        text("""
                SELECT r.id,r.conversation_seq,r.message,r.status,
                  (SELECT e.payload->'value' FROM agent_events e WHERE e.workflow_id=r.id
                    AND e.payload->>'name' IN ('business.task_spec','business.task_spec.resolved')
                    ORDER BY e.seq DESC LIMIT 1) AS spec
                FROM agent_runs r WHERE r.conversation_id=:id AND r.customer_id=:owner
                  AND (CAST(:before AS integer) IS NULL OR r.conversation_seq<CAST(:before AS integer))
                ORDER BY r.conversation_seq LIMIT 200
                """),
                        {"id": str(identifier), "owner": owner, "before": before_seq},
                    )
                )
                .mappings()
                .all()
            )
        turns = []
        for row in rows:
            spec = row["spec"] or {}
            ids = (
                TaskSpec.model_validate_json(__import__("json").dumps(spec)).order_ids
                if spec
                else []
            )
            turns.append(
                {
                    "seq": row["conversation_seq"],
                    "run_id": str(row["id"]),
                    "message": row["message"],
                    "status": row["status"],
                    "order_ids": ids,
                }
            )
        return safe_output(turns)

    async def delete(self, owner, identifier):
        async with self.engine.begin() as conn:
            row = await conn.scalar(
                text(
                    "UPDATE agent_conversations SET deleted_at=now() WHERE id=:id AND customer_id=:owner AND deleted_at IS NULL RETURNING id"
                ),
                {"id": str(identifier), "owner": owner},
            )
            if not row:
                raise HTTPException(404, "conversation_not_found")
        # Existing run/audit ledgers remain visible as tasks; they are no longer conversation recall.
        return {"deleted": True}
