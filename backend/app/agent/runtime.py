import asyncio
import time
from typing import TypedDict

from ag_ui.core import (
    CustomEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from opentelemetry import trace
from sqlalchemy import text

from app.agent.contracts import (
    ApprovalResume,
    Goal,
    Plan,
    ResumeInput,
    TaskSpec,
    compile_plan,
    progress_signature,
)
from app.agent.planner import Planner, PlanningError
from app.agent.tools import ReadGateway, ToolError, verify
from app.context.builder import ContextBuilder, ContextOverflow
from app.context.memory import MemoryService
from app.context.retrieval import KnowledgeService
from app.context.workflows import WorkflowService
from app.core.auth import Principal
from app.security.guards import safe_output, signals


class State(TypedDict, total=False):
    message: str
    spec: dict
    plan: dict
    completed: list[str]
    evidence: dict
    failures: dict
    attempts: dict
    current: str | None
    observation: dict | None
    error: str | None
    signatures: dict
    llm_calls: int
    total_tokens: int
    tool_calls: int
    replans: int
    active_seconds: float
    status: str
    result: dict
    proposals: dict
    knowledge_refs: list[str]
    memory_refs: list[str]
    context_stats: dict
    knowledge_error: str | None


def summarize(state):
    entries, lines = [], []
    goals = set(state.get("spec", {}).get("goals", []))
    committed_orders = {
        n["order_id"]
        for n in state.get("plan", {}).get("nodes", [])
        if n.get("risk") == "write" and n["id"] in state.get("evidence", {})
    }
    committed_money_orders = {
        n["order_id"]
        for n in state.get("plan", {}).get("nodes", [])
        if n["tool"] in {"submit_refund", "cancel_order"} and n["id"] in state.get("evidence", {})
    }
    receipts = {
        order: max(
            (
                state["evidence"][n["id"]]
                for n in state.get("plan", {}).get("nodes", [])
                if n["order_id"] == order
                and n.get("risk") == "write"
                and n["id"] in state["evidence"]
            ),
            key=lambda obs: obs["entity_version"],
        )
        for order in committed_orders
    }
    for node in state.get("plan", {}).get("nodes", []):
        key, tool, order = node["id"], node["tool"], node["order_id"]
        obs = state["evidence"].get(key)
        scoped_goals = next(
            (
                set(item["goals"])
                for item in state.get("spec", {}).get("order_goals", [])
                if item["order_id"] == order
            ),
            goals,
        )
        if not obs:
            continue
        f = obs["fields"]
        if node.get("risk") == "write":
            entries.append({"order_id": order, "kind": tool, "evidence": obs})
            label = {
                "create_return": "退货申请",
                "submit_refund": "退款",
                "cancel_order": "取消订单",
            }[tool]
            lines.append(
                f"订单 {order}：{label}已执行，操作回执 {f['operation_id']}，金额 {f['amount_minor'] / 100:.2f} 元（商家模拟）。"
            )
        elif tool == "business_query":
            entries.append({"order_id": order, "kind": tool, "evidence": obs})
            lines.append(
                f"订单 {order} 查询结果：" + __import__("json").dumps(f, ensure_ascii=False)
            )
        elif tool == "get_order" and "order" in scoped_goals:
            entries.append({"order_id": order, "kind": tool, "evidence": obs})
            status = receipts.get(order, {}).get("fields", {}).get("order_status", f["status"])
            lines.append(
                f"订单 {order}：{status}，实付 {f['paid_minor'] // 100}.{f['paid_minor'] % 100:02d} 元。"
            )
        elif tool == "get_logistics":
            entries.append({"order_id": order, "kind": tool, "evidence": obs})
            status = (
                "订单已取消"
                if receipts.get(order, {}).get("fields", {}).get("order_status") == "CANCELLED"
                else f["status"]
            )
            lines.append(f"订单 {order} 物流：{status}。")
        elif tool == "check_eligibility":
            entries.append({"order_id": order, "kind": tool, "evidence": obs})
            max_days = (
                state["evidence"]
                .get(f"{order}:get_policy", {})
                .get("fields", {})
                .get("max_delivered_days", 7)
            )
            lines.append(
                f"订单 {order}：{'操作前' if order in committed_orders else ''}{'符合' if f['eligible'] else '不符合'}演示商家{max_days}天退货政策。"
            )
        elif tool == "quote_refund":
            entries.append({"order_id": order, "kind": tool, "evidence": obs})
            if order in committed_money_orders:
                # A pre-write estimate is historical evidence. It cannot describe the final state.
                continue
            if f["available"]:
                inspection = (
                    "需退货验收；" if f.get("requires_return_inspection", True) else "已验收；"
                )
                lines.append(
                    f"订单 {order} 预计退款 {f['amount_minor'] // 100}.{f['amount_minor'] % 100:02d} 元，{inspection}尚未提交退款。"
                )
            else:
                lines.append(f"订单 {order} 不符合演示政策，无法生成退款试算；未提交退款。")
    failures = state.get("failures", {})
    if failures:
        lines.append(
            "部分步骤未完成："
            + "；".join(f"{key}（{value}）" for key, value in failures.items())
            + "。"
        )
    unsupported = goals & {"business_write", "unsupported"}
    if unsupported:
        committed = any(
            n.get("risk") == "write" and n["id"] in state["evidence"]
            for n in state.get("plan", {}).get("nodes", [])
        )
        lines.append(
            "未支持的业务操作需转人工处理；"
            + ("未执行这些未支持操作。" if committed else "未执行任何业务写操作。")
        )
    write_failures = {
        node["id"]: failures[node["id"]]
        for node in state.get("plan", {}).get("nodes", [])
        if node.get("risk") == "write" and node["id"] in failures
    }
    completed_writes = any(
        entry["kind"] in {"create_return", "submit_refund", "cancel_order"} for entry in entries
    )
    rejected = "approval_rejected" in write_failures.values()
    if rejected:
        lines.append("客户或主管拒绝了本次操作，已停止该操作。")
    if "business_precondition_failed" in write_failures.values():
        lines.append(
            "业务状态不满足操作条件；退款需先完成退货验收，取消需订单尚未发货。请按当前订单事实处理或转人工。"
        )
    status = (
        "CANCELLED"
        if rejected and not completed_writes
        else "PARTIAL"
        if write_failures and completed_writes
        else "HANDOFF"
        if write_failures
        else "HANDOFF"
        if unsupported
        else "PARTIAL"
        if failures and entries
        else "FAILED"
        if failures
        else "SUCCEEDED"
    )
    return {
        "status": status,
        "message": "\n".join(lines) or "请求需转人工处理。",
        "entries": entries,
        "failures": failures,
        "simulated_business": True,
        "operation_refs": [
            {
                "operation_id": value["operation_id"],
                "order_id": value["order_id"],
                "action": value["action"],
            }
            for value in state.get("proposals", {}).values()
        ],
        "usage": {
            k: state.get(k, 0) for k in ("llm_calls", "total_tokens", "tool_calls", "replans")
        },
    }


class Harness:
    def __init__(self, settings, store, run, planner=None, gateway=None):
        self.settings, self.store, self.run = settings, store, dict(run)
        self.planner = planner or Planner(settings)
        self.gateway = gateway or ReadGateway(store.engine, run["customer_id"])
        if gateway is None and settings.tool_routing_enabled:
            from app.business.gateway import Gateway

            self.gateway = Gateway(settings, store, self.run)
        self.prefix = str(run["invocation_id"])
        self.tracer = trace.get_tracer("agent-harness")
        self.actor = None

    async def check(self, state):
        await self.store.check(
            self.run["id"], self.run["fencing"], active_limit=self.settings.run_active_seconds
        )
        if state.get("active_seconds", 0) >= self.settings.run_active_seconds:
            raise PlanningError("active_time_budget")
        if state.get("llm_calls", 0) > 12 or state.get("total_tokens", 0) > 20000:
            raise PlanningError("model_budget")
        if state.get("tool_calls", 0) > 30:
            raise PlanningError("tool_budget")

    async def emit(self, key, event):
        await self.store.emit(self.run["id"], self.run["fencing"], f"{self.prefix}:{key}", event)

    def graph(self, saver):
        graph = StateGraph(State)
        for name in (
            "understand",
            "clarify",
            "plan",
            "select",
            "execute",
            "verify",
            "reflect",
            "finalize",
            "prepare_write",
            "wait_approval",
            "commit_write",
            "load_context",
            "prepare_context",
        ):
            graph.add_node(name, getattr(self, name))
        graph.add_edge(START, "understand")
        graph.add_conditional_edges(
            "understand", lambda s: "clarify" if s["spec"]["missing_inputs"] else "load_context"
        )
        graph.add_edge("clarify", "load_context")
        graph.add_edge("load_context", "plan")
        graph.add_edge("plan", "select")
        graph.add_edge("select", "prepare_context")
        graph.add_conditional_edges(
            "prepare_context",
            lambda s: (
                ("prepare_write" if self.node(s).risk == "write" else "execute")
                if s.get("current")
                else "finalize"
            ),
        )
        graph.add_conditional_edges(
            "prepare_write", lambda s: "reflect" if s.get("error") else "wait_approval"
        )
        graph.add_conditional_edges(
            "wait_approval", lambda s: "reflect" if s.get("error") else "commit_write"
        )
        graph.add_edge("commit_write", "verify")
        graph.add_edge("execute", "verify")
        graph.add_conditional_edges("verify", lambda s: "reflect" if s.get("error") else "select")
        graph.add_edge("reflect", "select")
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=saver)

    async def understand(self, state):
        await self.check(state)
        started = time.monotonic()
        await self.emit("understand:start", StepStartedEvent(step_name="理解任务"))
        cached = await self.store.event_payload(self.run["id"], f"{self.prefix}:task_spec")
        if cached:
            spec = TaskSpec.model_validate_json(__import__("json").dumps(cached["value"]))
            usage = cached["metadata"]["usage"]
        else:
            conversation = None
            if self.run.get("conversation_id"):
                from fastapi import HTTPException

                from app.context.conversation import ConversationService, compact_turns

                try:
                    turns = await ConversationService(self.store.engine).turns(
                        self.run["customer_id"],
                        self.run["conversation_id"],
                        self.run["conversation_seq"],
                    )
                except HTTPException as exc:
                    raise PlanningError("conversation_reference_unavailable") from exc
                conversation = compact_turns(turns)
                await self.emit(
                    "conversation_context",
                    CustomEvent(
                        name="context.conversation",
                        value={
                            "history_count": len(turns),
                            "recent_count": len(conversation["recent_turns"]),
                            "entity_count": len(conversation["entities"]),
                            "mode": "typed_ledger_recent_window",
                        },
                    ),
                )
            if conversation is None:
                spec, usage = await self.planner.parse(
                    state["message"], max_attempts=12 - state.get("llm_calls", 0)
                )
            else:
                spec, usage = await self.planner.parse(
                    state["message"],
                    conversation=conversation,
                    max_attempts=12 - state.get("llm_calls", 0),
                )
        await self.check(state)
        await self.emit(
            "task_spec",
            CustomEvent(
                name="business.task_spec",
                value=spec.model_dump(mode="json"),
                metadata={"usage": usage},
            ),
        )
        await self.emit("understand:end", StepFinishedEvent(step_name="理解任务"))
        return {
            "spec": spec.model_dump(mode="json"),
            "completed": [],
            "evidence": {},
            "failures": {},
            "attempts": {},
            "signatures": {},
            "tool_calls": 0,
            "replans": 0,
            "llm_calls": usage["llm_calls"],
            "total_tokens": usage["total_tokens"],
            "active_seconds": state.get("active_seconds", 0) + time.monotonic() - started,
            "proposals": {},
        }

    async def clarify(self, state):
        # Nothing with side effects precedes interrupt. This node restarts on resume.
        supplied = interrupt(
            {"question": "请提供或确认要处理的订单号（最多3个）。", "missing_inputs": ["order_id"]},
            response_schema=ResumeInput,
        )
        await self.check(state)
        spec = TaskSpec.model_validate_json(__import__("json").dumps(state["spec"]))
        if Goal.QUERY in spec.goals:
            # Missing-order requests have no bound query nodes yet. Reparse with explicit supplied
            # entities, then enforce the original goal and query text before compiling a plan.
            key = f"{self.prefix}:resolved_task_spec"
            cached = await self.store.event_payload(self.run["id"], key)
            started = time.monotonic()
            if cached:
                resolved = TaskSpec.model_validate_json(__import__("json").dumps(cached["value"]))
                usage = cached["metadata"]["usage"]
            else:
                resolved, usage = await self.planner.parse(
                    state["message"] + "\n补充订单号：" + "、".join(supplied.order_ids),
                    clarification={
                        "goals": [g.value for g in spec.goals],
                        "order_ids": supplied.order_ids,
                        "original_message": state["message"],
                    },
                    max_attempts=12 - state.get("llm_calls", 0),
                )
            if (
                set(resolved.order_ids) != set(supplied.order_ids)
                or set(resolved.goals) != set(spec.goals)
                or any(q.query not in state["message"] for q in resolved.queries)
            ):
                raise PlanningError("ungrounded_clarification_scope", usage)
            await self.check(state)
            await self.emit(
                "resolved_task_spec",
                CustomEvent(
                    name="business.task_spec.resolved",
                    value=resolved.model_dump(mode="json"),
                    metadata={"usage": usage},
                ),
            )
            return {
                "spec": resolved.model_dump(mode="json"),
                "llm_calls": state["llm_calls"] + usage["llm_calls"],
                "total_tokens": state["total_tokens"] + usage["total_tokens"],
                "active_seconds": state.get("active_seconds", 0) + time.monotonic() - started,
            }
        spec.order_ids, spec.missing_inputs = supplied.order_ids, []
        return {"spec": spec.model_dump(mode="json")}

    async def plan(self, state):
        await self.check(state)
        spec = TaskSpec.model_validate_json(__import__("json").dumps(state["spec"]))
        plan = compile_plan(spec)
        if self.settings.context_engineering_enabled:
            actor = await self.context_actor()
            reused, info = (None, None)
            if self.run.get("dataset_partition", "development") == "development":
                reused, info = await WorkflowService(self.settings, self.store.engine).match(
                    actor, spec
                )
            if reused:
                plan = reused
                await self.emit(
                    "workflow_reused", CustomEvent(name="context.workflow.reused", value=info)
                )
        metadata = {}
        if self.settings.context_engineering_enabled:
            from app.context.workflows import tool_versions

            async with self.store.engine.connect() as conn:
                policy_version = await conn.scalar(
                    text("SELECT version FROM merchant_policies WHERE tenant_id=:tenant"),
                    {"tenant": actor.tenant_id},
                )
            metadata = {
                "tool_versions": tool_versions(),
                "policy_version": policy_version,
                "dataset_partition": self.run.get("dataset_partition", "development"),
            }
        await self.emit(
            "plan",
            CustomEvent(
                name="business.plan", value=plan.model_dump(mode="json"), metadata=metadata
            ),
        )
        return {"plan": plan.model_dump(mode="json")}

    async def context_actor(self):
        # Resolve namespace from current authoritative account, not model state or request text.
        async with self.store.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT id,role,tenant_id FROM customer_accounts WHERE id=:owner AND active"
                        ),
                        {"owner": self.run["customer_id"]},
                    )
                )
                .mappings()
                .first()
            )
        if not row or row["role"] != "customer":
            raise PlanningError("context_identity_revoked")
        return Principal(row["id"], row["role"], row["tenant_id"])

    async def load_context(self, state):
        await self.check(state)
        if not self.settings.context_engineering_enabled:
            return {
                "memory_refs": [],
                "knowledge_refs": [],
                "knowledge_error": "context_disabled"
                if "knowledge" in state["spec"]["goals"]
                else None,
            }
        actor = await self.context_actor()
        started = time.monotonic()
        memories = []
        if self.run.get("dataset_partition", "development") == "development":
            memories = await MemoryService(self.settings, self.store.engine).retrieve(
                actor, state["message"]
            )
        references, error, reports = [], None, []
        from fastapi import HTTPException

        for query in state["spec"].get("queries", []):
            if query["order_id"]:
                continue
            try:
                retrieved = await KnowledgeService(self.settings, self.store.engine).search(
                    actor, query["query"], 3
                )
                references.extend(item["chunk_id"] for item in retrieved["items"])
                reports.append(retrieved["diagnostics"])
            except HTTPException as exc:
                error = exc.detail
        await self.check(state)
        await self.emit(
            "retrieval",
            CustomEvent(
                name="context.retrieval",
                value={
                    "memory_count": len(memories),
                    "knowledge_count": len(set(references)),
                    "diagnostics": reports,
                    "error": error,
                    "source_refs": ["knowledge:" + key for key in dict.fromkeys(references)],
                },
            ),
        )
        return {
            "memory_refs": [item["memory_id"] for item in memories],
            "knowledge_refs": list(dict.fromkeys(references)),
            "knowledge_error": error,
            "active_seconds": state.get("active_seconds", 0) + time.monotonic() - started,
        }

    async def prepare_context(self, state):
        await self.check(state)
        if not self.settings.context_engineering_enabled:
            return {}
        actor = await self.context_actor()
        advisory = await MemoryService(self.settings, self.store.engine).fetch(
            actor, state.get("memory_refs", [])
        )
        knowledge = await KnowledgeService(self.settings, self.store.engine).fetch(
            actor, state.get("knowledge_refs", [])
        )
        advisory.extend(
            {
                "trust_class": "untrusted_retrieved_data",
                "evidence_ref": "knowledge:" + str(row["id"]),
                "content": row["content"],
            }
            for row in knowledge
        )
        try:
            packet, stats = ContextBuilder(self.settings).build(state, actor, advisory)
        except ContextOverflow as exc:
            raise PlanningError(str(exc)) from None
        # Only in-memory model context holds text. Checkpoint/audit store refs and budget metadata.
        if hasattr(self.gateway, "router"):
            self.gateway.router.context_state = state
            self.gateway.router.context_advisory = packet["advisory"]
            self.gateway.router.context_actor = actor
        key = (
            "context:"
            + (state.get("current") or "final")
            + ":"
            + str(sum(state.get("attempts", {}).values()))
        )
        await self.emit(key, CustomEvent(name="context.budget", value=stats))
        return {"context_stats": stats}

    async def select(self, state):
        await self.check(state)
        failures, completed = dict(state["failures"]), set(state["completed"])
        for node in state["plan"]["nodes"]:
            if node["id"] in completed or node["id"] in failures:
                continue
            if set(node["dependencies"]) & set(failures):
                failures[node["id"]] = "dependency_failed"
                continue
            if set(node["dependencies"]) <= completed:
                return {"current": node["id"], "failures": failures}
        return {"current": None, "failures": failures}

    def node(self, state):
        plan = Plan.model_validate_json(__import__("json").dumps(state["plan"]))
        return next(n for n in plan.nodes if n.id == state["current"])

    async def execute(self, state):
        await self.check(state)
        if state.get("tool_calls", 0) >= 30:
            raise PlanningError("tool_budget")
        if hasattr(self.gateway, "router"):
            self.gateway.router.available_llm_calls = max(0, 12 - state.get("llm_calls", 0))
        started, node = time.monotonic(), self.node(state)
        attempts = dict(state["attempts"])
        attempt = attempts[node.id] = attempts.get(node.id, 0) + 1
        key, call_id = f"{node.id}:{attempt}", f"{self.prefix}:{node.id}:{attempt}"
        import json

        await self.emit(key + ":start", StepStartedEvent(step_name=node.id))
        await self.emit(
            key + ":tool:start", ToolCallStartEvent(tool_call_id=call_id, tool_call_name=node.tool)
        )
        await self.emit(
            key + ":tool:args",
            ToolCallArgsEvent(tool_call_id=call_id, delta=json.dumps({"order_id": node.order_id})),
        )
        # AG-UI END closes argument streaming; RESULT carries the executed result.
        await self.emit(key + ":tool:end", ToolCallEndEvent(tool_call_id=call_id))
        error, observation = None, None
        cached = await self.store.event_payload(self.run["id"], f"{self.prefix}:{key}:tool:result")
        with self.tracer.start_as_current_span(
            "agent.tool",
            attributes={
                "agent.workflow_id": str(self.run["id"]),
                "agent.node_id": node.id,
                "agent.tool_name": node.tool,
                "agent.fencing": self.run["fencing"],
            },
        ):
            try:
                remaining = self.settings.run_active_seconds - state.get("active_seconds", 0)
                async with asyncio.timeout(
                    min(45 if self.settings.tool_routing_enabled else 10, max(0.001, remaining))
                ):
                    if cached:
                        restored = json.loads(cached["content"])
                        if "error" in restored:
                            error = restored["error"]
                        else:
                            observation = restored
                    else:
                        if self.settings.tool_routing_enabled:
                            observation = await self.gateway.execute(
                                node, state["evidence"], node.query or state["message"]
                            )
                        else:
                            observation = await self.gateway.execute(node, state["evidence"])
            except ToolError as exc:
                error = exc.code
            except TimeoutError:
                error = "upstream_unavailable"
        await self.check(state)
        if observation:
            if signals(observation.get("fields", {})):
                observation, error = None, "unsafe_tool_content"
            else:
                observation = safe_output(observation)
        info = getattr(self.gateway, "routing_info", {}) if not cached else {}
        if cached and self.settings.tool_routing_enabled:
            route = await self.store.event_payload(self.run["id"], f"{self.prefix}:{key}:route")
            info = route["value"] if route else {}
        if info:
            await self.emit(key + ":route", CustomEvent(name="business.tool_route", value=info))
        await self.emit(
            key + ":tool:result",
            ToolCallResultEvent(
                message_id=call_id + ":result",
                tool_call_id=call_id,
                content=json.dumps(observation or {"error": error}, ensure_ascii=False),
            ),
        )
        return {
            "attempts": attempts,
            "observation": observation,
            "error": error,
            "tool_calls": state["tool_calls"] + 1,
            "llm_calls": state.get("llm_calls", 0) + info.get("llm_calls", 0),
            "total_tokens": state.get("total_tokens", 0) + info.get("total_tokens", 0),
            "active_seconds": state.get("active_seconds", 0) + time.monotonic() - started,
        }

    async def prepare_write(self, state):
        await self.check(state)
        node = self.node(state)
        await self.emit(
            f"{node.id}:prepare:start", StepStartedEvent(step_name=node.id + ":prepare")
        )
        attempts = {**state["attempts"], node.id: state["attempts"].get(node.id, 0) + 1}
        try:
            if not self.settings.tool_routing_enabled:
                raise ToolError("write_gateway_disabled")
            proposal = await self.gateway.prepare(node, state["evidence"])
            value = {
                key: proposal[key]
                for key in (
                    "order_id",
                    "action",
                    "amount_minor",
                    "arg_hash",
                    "entity_version",
                    "policy_version",
                    "requires_supervisor",
                    "revision",
                    "status",
                )
            }
            value.update(
                operation_id=str(proposal["operation_id"]),
                expires_at=proposal["expires_at"].isoformat(),
            )
            await self.emit(
                f"{node.id}:proposal:{proposal['operation_id']}",
                CustomEvent(name="business.approval.requested", value=value),
            )
            await self.emit(
                f"{node.id}:prepare:end", StepFinishedEvent(step_name=node.id + ":prepare")
            )
            return {
                "proposals": {**state["proposals"], node.id: value},
                "attempts": attempts,
                "error": None,
            }
        except ToolError as exc:
            await self.emit(
                f"{node.id}:prepare:end", StepFinishedEvent(step_name=node.id + ":prepare")
            )
            return {"error": exc.code, "attempts": attempts}

    async def wait_approval(self, state):
        proposal = state["proposals"][state["current"]]
        # Persistent proposal is created in the preceding node. No side effect before interrupt.
        interrupt(
            {
                "question": "请审查并确认业务操作；大额资金操作还需主管审批。",
                "kind": "approval",
                "proposal": proposal,
            },
            response_schema=ApprovalResume,
        )
        await self.check(state)
        actual = await self.gateway.proposal(proposal["operation_id"])
        if not actual or actual["status"] != "APPROVED":
            return {
                "error": "approval_rejected"
                if actual and actual["status"] == "REJECTED"
                else "approval_invalidated"
            }
        return {"error": None}

    async def commit_write(self, state):
        await self.check(state)
        if state.get("tool_calls", 0) + 2 > 30:
            raise PlanningError("tool_budget")
        node = self.node(state)
        key = f"{node.id}:{state['attempts'][node.id]}"
        call_id = f"{self.prefix}:{key}"
        proposal = await self.gateway.proposal(state["proposals"][node.id]["operation_id"])
        await self.emit(key + ":start", StepStartedEvent(step_name=node.id))
        await self.emit(
            key + ":tool:start", ToolCallStartEvent(tool_call_id=call_id, tool_call_name=node.tool)
        )
        await self.emit(
            key + ":tool:args",
            ToolCallArgsEvent(
                tool_call_id=call_id,
                delta=__import__("json").dumps(
                    {"order_id": node.order_id, "operation_id": str(proposal["operation_id"])}
                ),
            ),
        )
        await self.emit(key + ":tool:end", ToolCallEndEvent(tool_call_id=call_id))
        await self.emit(
            f"{node.id}:dispatch",
            CustomEvent(
                name="business.operation.dispatch",
                value={"operation_id": str(proposal["operation_id"]), "action": node.tool},
            ),
        )
        try:
            receipt, info = await self.gateway.commit(node, proposal)
            await self.emit(
                f"{node.id}:receipt",
                CustomEvent(name="business.operation.receipt", value={"receipt": receipt, **info}),
            )
            await self.emit(
                key + ":tool:result",
                ToolCallResultEvent(
                    message_id=call_id + ":result",
                    tool_call_id=call_id,
                    content=__import__("json").dumps(receipt, ensure_ascii=False),
                ),
            )
            return {"observation": receipt, "error": None, "tool_calls": state["tool_calls"] + 2}
        except ToolError as exc:
            await self.emit(
                key + ":tool:result",
                ToolCallResultEvent(
                    message_id=call_id + ":result",
                    tool_call_id=call_id,
                    content=__import__("json").dumps({"error": exc.code}),
                ),
            )
            return {"error": exc.code, "observation": None, "tool_calls": state["tool_calls"] + 2}

    async def verify(self, state):
        await self.check(state)
        node = self.node(state)
        error = state.get("error")
        if not error:
            try:
                verify(node, state["observation"], state["evidence"])
            except ToolError as exc:
                error = exc.code
        key = f"{node.id}:{state['attempts'][node.id]}"
        await self.emit(
            key + ":verified",
            CustomEvent(
                name="business.verification",
                value={"node_id": node.id, "passed": error is None, "error": error},
            ),
        )
        await self.emit(key + ":end", StepFinishedEvent(step_name=node.id))
        if error:
            return {"error": error}
        return {
            "completed": state["completed"] + [node.id],
            "evidence": {**state["evidence"], node.id: state["observation"]},
            "observation": None,
            "error": None,
        }

    async def reflect(self, state):
        await self.check(state)
        started, node, error = time.monotonic(), self.node(state), state["error"]
        remaining = [n["id"] for n in state["plan"]["nodes"] if n["id"] not in state["completed"]]
        signature = progress_signature(
            state["plan"]["version"], remaining, state["evidence"], error
        )
        signatures = dict(state["signatures"])
        signatures[signature] = signatures.get(signature, 0) + 1
        recoverable = (
            node.risk == "read"
            or (error == "stale_environment" and node.id not in state.get("proposals", {}))
        ) and error in {
            "stale_environment",
            "upstream_unavailable",
            "expired_evidence",
        }
        action, tokens, calls = "handoff", 0, 0
        critique_usage = {"proposal_validated": False, "decision_source": "deterministic_stop"}
        bounded = (
            state["replans"] < 3 and state["attempts"][node.id] < 3 and signatures[signature] < 2
        )
        if recoverable and bounded:
            code = "stale_environment" if error == "expired_evidence" else error
            cached = await self.store.event_payload(
                self.run["id"], f"{self.prefix}:{node.id}:{state['attempts'][node.id]}:reflection"
            )
            if cached:
                cached = cached["value"]
                action = cached["action"]
                critique_usage = {
                    key: cached[key]
                    for key in (
                        "llm_calls",
                        "total_tokens",
                        "proposal_validated",
                        "decision_source",
                    )
                }
            else:
                remaining_calls = 12 - state.get("llm_calls", 0)
                if remaining_calls <= 0:
                    action, critique_usage = (
                        "handoff",
                        {
                            "llm_calls": 0,
                            "total_tokens": 0,
                            "proposal_validated": False,
                            "decision_source": "model_budget",
                        },
                    )
                elif remaining_calls == 1:
                    action, critique_usage = await self.planner.critique(code, max_attempts=1)
                else:
                    action, critique_usage = await self.planner.critique(code)
            tokens, calls = critique_usage["total_tokens"], critique_usage["llm_calls"]
        failures, evidence, completed = (
            dict(state["failures"]),
            dict(state["evidence"]),
            list(state["completed"]),
        )
        if action == "refresh_order":
            affected = {
                n["id"]
                for n in state["plan"]["nodes"]
                if n["order_id"] == node.order_id and n.get("risk", "read") == "read"
            }
            completed = [key for key in completed if key not in affected]
            evidence = {key: value for key, value in evidence.items() if key not in affected}
        elif action == "retry_read":
            await asyncio.sleep(min(0.25 * state["attempts"][node.id], 1))
        else:
            failures[node.id] = "no_progress" if signatures[signature] >= 2 else error
        await self.emit(
            f"{node.id}:{state['attempts'][node.id]}:reflection",
            CustomEvent(
                name="business.reflection",
                value={
                    "node_id": node.id,
                    "issue": error,
                    "action": action,
                    "evidence_refs": [item["evidence_id"] for item in state["evidence"].values()],
                    "no_progress_count": signatures[signature],
                    **critique_usage,
                },
            ),
        )
        return {
            "failures": failures,
            "evidence": evidence,
            "completed": completed,
            "signatures": signatures,
            "error": None,
            "observation": None,
            "replans": state["replans"] + (action != "handoff"),
            "llm_calls": state["llm_calls"] + calls,
            "total_tokens": state["total_tokens"] + tokens,
            "active_seconds": state.get("active_seconds", 0) + time.monotonic() - started,
        }

    async def finalize(self, state):
        await self.check(state)
        final_started = time.monotonic()
        result = summarize(state)
        if "knowledge" in state.get("spec", {}).get("goals", []):
            rows = []
            if self.settings.context_engineering_enabled:
                rows = await KnowledgeService(self.settings, self.store.engine).fetch(
                    await self.context_actor(), state.get("knowledge_refs", [])
                )
            citations = [KnowledgeService.citation(row) for row in rows]
            answer_usage = {"llm_calls": 0, "total_tokens": 0, "mode": "no_evidence"}
            quotes = None
            if citations:
                from app.context.answer import KnowledgeAnswer, generate, verify_quotes

                cached = await self.store.event_payload(
                    self.run["id"], f"{self.prefix}:knowledge_answer"
                )
                if cached:
                    answer_usage = cached["metadata"]
                    quotes = cached["value"]["quotes"]
                else:
                    quotes, answer_usage = await generate(
                        self.settings, await self.context_actor(), state, citations
                    )
                current_rows = await KnowledgeService(self.settings, self.store.engine).fetch(
                    await self.context_actor(), state.get("knowledge_refs", [])
                )
                citations = [KnowledgeService.citation(row) for row in current_rows]
                if quotes is not None:
                    try:
                        verify_quotes(
                            KnowledgeAnswer.model_validate_json(
                                __import__("json").dumps({"quotes": quotes})
                            ),
                            citations,
                        )
                    except ValueError:
                        quotes = []
                await self.check(state)
                await self.emit(
                    "knowledge_answer",
                    CustomEvent(
                        name="context.answer", value={"quotes": quotes}, metadata=answer_usage
                    ),
                )
            for key in ("llm_calls", "total_tokens"):
                result["usage"][key] += answer_usage[key]
            result["knowledge_citations"] = citations
            result["knowledge_answer"] = {"quotes": quotes, **answer_usage}
            if citations:
                excerpts = [
                    f"[{index}] {item['title']}（v{item['version']}，参考资料）：{item['content']}"
                    for index, item in enumerate(citations, 1)
                ]
                if quotes is not None:
                    by_id = {item["chunk_id"]: index for index, item in enumerate(citations, 1)}
                    excerpts = [f"[{by_id[q['chunk_id']]}] {q['quote']}" for q in quotes]
                prefix = result["message"] if result["entries"] or result["failures"] else ""
                result["message"] = "\n".join(
                    [
                        prefix,
                        "知识库引用回答（仅作参考；订单资格与金额以当前商家工具为准）：",
                        *excerpts,
                    ]
                ).strip()
                if quotes == []:
                    result["message"] += "\n当前证据未能支持该知识问题，请补充资料或转人工。"
            else:
                result["message"] += "\n没有可授权、有效的知识证据，请补充资料或转人工。"
            if not citations or quotes == [] or state.get("knowledge_error"):
                result["status"] = "PARTIAL" if result["entries"] else "HANDOFF"
                result["knowledge_error"] = (
                    state.get("knowledge_error") or "no_current_knowledge_evidence"
                )
        if self.settings.context_engineering_enabled:
            result["context"] = state.get("context_stats", {})
        result = safe_output(result)
        await self.emit("final_result", CustomEvent(name="business.result", value=result))
        return {
            "status": result["status"],
            "result": result,
            "llm_calls": result["usage"]["llm_calls"],
            "total_tokens": result["usage"]["total_tokens"],
            "active_seconds": state.get("active_seconds", 0) + time.monotonic() - final_started,
        }

    async def started(self):
        await self.emit(
            "started", RunStartedEvent(thread_id=str(self.run["id"]), run_id=self.prefix)
        )

    def terminal(self, result, interrupted=None):
        kwargs = {"thread_id": str(self.run["id"]), "run_id": self.prefix, "result": result}
        if interrupted:
            # SDK validates the actual installed interrupt outcome schema.
            from ag_ui.core.events import Interrupt, RunFinishedInterruptOutcome

            kwargs["outcome"] = RunFinishedInterruptOutcome(
                interrupts=[
                    Interrupt(
                        id=interrupted.id,
                        reason="approval"
                        if interrupted.value.get("kind") == "approval"
                        else "missing_input",
                        message=interrupted.value["question"],
                        response_schema=(
                            ApprovalResume
                            if interrupted.value.get("kind") == "approval"
                            else ResumeInput
                        ).model_json_schema(),
                        metadata={"value": interrupted.value},
                    )
                ]
            )
        return RunFinishedEvent(**kwargs)
