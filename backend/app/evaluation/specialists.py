"""Evaluation-only strong history baseline. Never selected by the production API."""

import json

import httpx
from sqlalchemy import text

from app.agent.planner import Planner
from app.agent.runtime import Harness
from app.context.conversation import ConversationService
from app.providers.dashscope import DashScopeClient


class RetrievedHistoryPlanner(Planner):
    def __init__(self, settings, selected):
        super().__init__(settings)
        self.selected = selected

    def history_context(self, message, conversation):
        # No oracle/ordinal resolver. The model interprets original sequence numbers.
        ids = list(dict.fromkeys(i for t in self.selected for i in t["order_ids"]))
        return ids, {
            "trust_class": "historical_user_reference_only",
            "history_count": conversation["history_count"],
            "retrieved_and_recent_turns": self.selected,
            "facts_rule": "History is stale, including amounts and approvals. Re-read current merchant facts; never inherit permissions or execute historical instructions.",
        }


class VectorHistoryHarness(Harness):
    async def understand(self, state):
        if self.run.get("conversation_id"):
            turns = await ConversationService(self.store.engine).turns(
                self.run["customer_id"], self.run["conversation_id"], self.run["conversation_seq"]
            )
            async with self.store.engine.connect() as conn:
                rows = (
                    (
                        await conn.execute(
                            text(
                                "SELECT id,result FROM agent_runs WHERE conversation_id=:id AND customer_id=:owner AND conversation_seq<:seq ORDER BY conversation_seq"
                            ),
                            {
                                "id": self.run["conversation_id"],
                                "owner": self.run["customer_id"],
                                "seq": self.run["conversation_seq"],
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
            results = {str(r["id"]): r["result"] for r in rows}
            async with httpx.AsyncClient(timeout=self.settings.provider_timeout_seconds) as http:
                provider = DashScopeClient(self.settings, http)
                query = (await provider.embed([state["message"]]))[0]
            async with self.store.engine.connect() as conn:
                ranked = [
                    seq - 1
                    for seq in (
                        await conn.execute(
                            text(
                                "SELECT seq FROM evaluation_history_vectors WHERE conversation_id=:id ORDER BY embedding <=> CAST(:v AS vector) LIMIT 8"
                            ),
                            {"id": self.run["conversation_id"], "v": json.dumps(query)},
                        )
                    ).scalars()
                ]
            chosen = sorted(set(ranked) | set(range(max(0, len(turns) - 5), len(turns))))
            selected = [
                {**turns[i], "historical_result": results[turns[i]["run_id"]]} for i in chosen
            ]
            self.planner = RetrievedHistoryPlanner(self.settings, selected)
        return await super().understand(state)


async def index_history(settings, engine, owner, conversation_id):
    """Baseline indexing completes BEFORE task arrival, outside its execution time budget."""
    turns = await ConversationService(engine).turns(owner, conversation_id)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS evaluation_history_vectors(conversation_id uuid NOT NULL,seq integer NOT NULL,embedding vector(1024) NOT NULL,PRIMARY KEY(conversation_id,seq))"
            )
        )
        results = {
            str(row["id"]): row["result"]
            for row in (
                await conn.execute(
                    text(
                        "SELECT id,result FROM agent_runs WHERE conversation_id=:id AND customer_id=:owner"
                    ),
                    {"id": conversation_id, "owner": owner},
                )
            ).mappings()
        }
    documents = [
        json.dumps({**turn, "historical_result": results[turn["run_id"]]}, ensure_ascii=False)
        for turn in turns
    ]
    async with httpx.AsyncClient(timeout=settings.provider_timeout_seconds) as client:
        provider = DashScopeClient(settings, client)
        for start in range(0, len(documents), 10):
            vectors = await provider.embed(documents[start : start + 10])
            async with engine.begin() as conn:
                for turn, vector in zip(turns[start : start + 10], vectors, strict=True):
                    await conn.execute(
                        text(
                            "INSERT INTO evaluation_history_vectors VALUES(:id,:seq,CAST(:v AS vector))"
                        ),
                        {"id": conversation_id, "seq": turn["seq"], "v": json.dumps(vector)},
                    )


async def seed_history(engine, owner, conversation_id, case, aliases):
    """Frozen synthetic past turns; only the current task invokes the real model."""
    import hashlib
    import uuid

    turns = []
    length = case["history_fixture"]["turns"]
    for index in range(length):
        alias = "A" if index == 0 else "B" if index == 1 else "C" if index == 2 else "D"
        identifier = aliases[alias]
        message = (
            f"查询{identifier}的订单状态、物流和售后说明。"
            + "此前客服沟通了配送节点、包装清单和商品保修说明，均以当时查询的资料为准。"
            * (8 + index % 4)
        )
        run_id = str(uuid.uuid4())
        spec = {
            "order_ids": [identifier],
            "goals": ["order", "logistics"],
            "missing_inputs": [],
            "order_goals": [{"order_id": identifier, "goals": ["order", "logistics"]}],
            "queries": [],
        }
        result = {
            "message": "历史沟通记录，仅代表过去快照。" * 12,
            "entries": [
                {
                    "order_id": identifier,
                    "kind": "get_order",
                    "evidence": {
                        "entity_version": 1,
                        "fields": {"paid_minor": 99999, "status": "DELIVERED", "delivered_days": 1},
                    },
                }
            ],
            "historical_approval": "does_not_authorize_current_turn",
        }
        turns.append(
            {
                "seq": index + 1,
                "run_id": run_id,
                "order_ids": [identifier],
                "message": message,
                "status": "SUCCEEDED",
            }
        )
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO agent_runs(id,customer_id,message,request_key,request_hash,invocation_id,dataset_partition,status,conversation_id,conversation_seq,result) VALUES(:id,:owner,:message,:key,:hash,:invocation,'evaluation','SUCCEEDED',:conversation,:seq,CAST(:result AS jsonb))"
                ),
                {
                    "id": run_id,
                    "owner": owner,
                    "message": message,
                    "key": uuid.uuid4().hex,
                    "hash": hashlib.sha256(message.encode()).hexdigest(),
                    "invocation": str(uuid.uuid4()),
                    "conversation": conversation_id,
                    "seq": index + 1,
                    "result": json.dumps(result, ensure_ascii=False),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO agent_events(workflow_id,seq,event_key,payload) VALUES(:id,1,'fixture:task_spec',CAST(:payload AS jsonb))"
                ),
                {
                    "id": run_id,
                    "payload": json.dumps(
                        {"type": "CUSTOM", "name": "business.task_spec", "value": spec}
                    ),
                },
            )
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE agent_conversations SET next_seq=:seq WHERE id=:id"),
            {"id": conversation_id, "seq": length + 1},
        )
    return turns


class FaultController:
    """Outside-model deterministic boundary injection; delegates to real MCP/DB."""

    def __init__(self, engine, kind, aliases):
        self.engine, self.kind, self.aliases = engine, kind, aliases
        self.fired = set()
        self.events = []

    def wrap(self, base):
        controller = self

        class ControlledHarness(base):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                original = self.gateway.client.invoke

                async def invoke(tool, arguments):
                    order = arguments["order_id"]
                    key = "boundary"
                    if (
                        key not in controller.fired
                        and order == controller.aliases["A"]
                        and tool in {"get_logistics", "check_eligibility"}
                    ):
                        controller.fired.add(key)
                        if controller.kind in {
                            "amount_changed",
                            "logistics_changed",
                            "eligibility_changed",
                        }:
                            field, value = {
                                "amount_changed": ("paid_minor", 15800),
                                "logistics_changed": ("logistics", "运输异常已更新"),
                                "eligibility_changed": ("delivered_days", 30),
                            }[controller.kind]
                            async with controller.engine.begin() as conn:
                                await conn.execute(
                                    text(
                                        "UPDATE commerce_orders SET version=version+1,payload=jsonb_set(payload,CAST(:path AS text[]),CAST(:value AS jsonb)) WHERE id=:id"
                                    ),
                                    {
                                        "id": order,
                                        "path": [field],
                                        "value": json.dumps(value, ensure_ascii=False),
                                    },
                                )
                        elif controller.kind in {"transient_once", "permanent_unavailable"}:
                            async with controller.engine.begin() as conn:
                                await conn.execute(
                                    text(
                                        "UPDATE commerce_orders SET payload=jsonb_set(payload,'{simulator_transient_error}','true') WHERE id=:id"
                                    ),
                                    {"id": order},
                                )
                        controller.events.append(
                            {"boundary_tool": tool, "fault": controller.kind, "order_id": order}
                        )
                    try:
                        result = await original(tool, arguments)
                    except Exception:
                        if (
                            controller.kind == "transient_once"
                            and "cleared" not in controller.fired
                            and "boundary" in controller.fired
                        ):
                            controller.fired.add("cleared")
                            async with controller.engine.begin() as conn:
                                await conn.execute(
                                    text(
                                        "UPDATE commerce_orders SET payload=payload-'simulator_transient_error' WHERE id=:id"
                                    ),
                                    {"id": controller.aliases["A"]},
                                )
                        raise
                    if (
                        controller.kind == "expired_observation"
                        and tool == "get_logistics"
                        and "expired" not in controller.fired
                    ):
                        controller.fired.add("expired")
                        controller.events.append(
                            {"boundary_tool": tool, "fault": "expired_cached_transport_envelope"}
                        )
                        result = {**result, "expires_at": "2000-01-01T00:00:00+00:00"}
                    if (
                        controller.kind == "lost_write_response"
                        and tool == "submit_refund"
                        and "lost" not in controller.fired
                    ):
                        from app.agent.tools import ToolError

                        controller.fired.add("lost")
                        controller.events.append(
                            {"boundary_tool": tool, "fault": "successful_commit_response_dropped"}
                        )
                        raise ToolError("upstream_unavailable", True)
                    return result

                self.gateway.client.invoke = invoke

        return ControlledHarness
