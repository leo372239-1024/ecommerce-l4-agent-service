import json
import uuid

import pytest
from ag_ui.core import RunFinishedEvent
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import ValidationError

from app.agent.contracts import (
    Goal,
    OrderGoals,
    Plan,
    QuerySpec,
    ResumeInput,
    TaskSpec,
    compile_plan,
)
from app.agent.planner import PlanningError, grounded_spec, validation_reason
from app.agent.runtime import Harness
from app.agent.tools import ToolError, envelope, verify
from app.core.config import Settings
from app.main import create_app


def config(**kw):
    return Settings(_env_file=None, _secrets_dir=None, app_env="test", **kw)


def test_grounding_never_invents_or_matches_order_prefix():
    for message in ("查订单", "查ORD-10010"):
        with pytest.raises(PlanningError):
            grounded_spec('{"order_ids":["ORD-1001"],"goals":["order"]}', message)


def test_model_cannot_propose_identity_tools_or_permissions():
    with pytest.raises(ValidationError):
        grounded_spec('{"order_ids":[],"goals":["order"],"customer_id":"bob"}', "查询")


def test_missing_input_is_computed_from_grounded_spec():
    spec = grounded_spec('{"order_ids":[],"goals":["logistics"],"missing_inputs":[]}', "查物流")
    assert spec.missing_inputs == ["order_id"]


def test_multi_goal_dag_enforces_mandatory_dependencies():
    plan = compile_plan(
        TaskSpec(order_ids=["ORD-1001", "ORD-1002"], goals=[Goal.LOGISTICS, Goal.QUOTE])
    )
    assert len(plan.nodes) == 10
    raw = plan.model_dump(mode="json")
    raw["nodes"][-1]["dependencies"] = []
    with pytest.raises(ValidationError):
        Plan.model_validate_json(json.dumps(raw))


def test_goals_are_scoped_to_orders_and_not_cross_applied():
    spec = TaskSpec(
        order_ids=["ORD-1001", "ORD-1002"],
        goals=[Goal.LOGISTICS, Goal.QUOTE],
        order_goals=[
            OrderGoals(order_id="ORD-1001", goals=[Goal.QUOTE]),
            OrderGoals(order_id="ORD-1002", goals=[Goal.LOGISTICS]),
        ],
    )
    plan = compile_plan(spec)
    assert len(plan.nodes) == 6
    assert "ORD-1001:get_logistics" not in {n.id for n in plan.nodes}
    assert "ORD-1002:quote_refund" not in {n.id for n in plan.nodes}


def test_incomplete_order_scope_is_rejected_instead_of_silently_dropping_goals():
    with pytest.raises(ValidationError):
        TaskSpec(
            order_ids=["ORD-1001", "ORD-1002"],
            goals=[Goal.LOGISTICS],
            order_goals=[OrderGoals(order_id="ORD-1001", goals=[Goal.LOGISTICS])],
        )


def test_resume_schema_forbids_injected_state():
    for value in ({"order_ids": ["../private"]}, {"order_ids": ["ORD-1001"], "approved": True}):
        with pytest.raises(ValidationError):
            ResumeInput.model_validate(value)


@pytest.mark.parametrize(
    "amount,currency", [(True, "CNY"), (-1, "CNY"), (12900, "USD"), (100, "CNY")]
)
def test_money_verifier_rejects_untrusted_quote(amount, currency):
    node = compile_plan(TaskSpec(order_ids=["ORD-1001"], goals=[Goal.QUOTE])).nodes[-1]
    obs = envelope("ORD-1001", 1, {"available": True, "amount_minor": amount, "currency": currency})
    facts = {"ORD-1001:get_order": envelope("ORD-1001", 1, {"paid_minor": 12900})}
    with pytest.raises(ToolError):
        verify(node, obs, facts)


class MemoryStore:
    engine = None

    def __init__(self):
        self.events = {}
        self.checks = 0

    async def check(self, *_args, **_kwargs):
        self.checks += 1

    async def emit(self, _id, _fence, key, event):
        self.events.setdefault(key, event)

    async def event_payload(self, _id, key):
        event = self.events.get(key)
        return event.model_dump(mode="json", by_alias=True) if event else None


class StubPlanner:
    def __init__(self, spec):
        self.spec = spec
        self.calls = 0

    async def parse(self, _message, **_kwargs):
        self.calls += 1
        return self.spec, {"llm_calls": 1, "total_tokens": 20}

    async def critique(self, code):
        return ("refresh_order" if code == "stale_environment" else "retry_read"), {
            "llm_calls": 1,
            "total_tokens": 10,
            "proposal_validated": True,
            "decision_source": "validated_model_proposal",
        }


class StubGateway:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    async def execute(self, node, evidence):
        self.calls.append(node.tool)
        if self.failure:
            raise ToolError(self.failure, True)
        fields = {
            "status": "DELIVERED",
            "paid_minor": 12900,
            "currency": "CNY",
            "delivered_days": 3,
        }
        if node.tool == "get_logistics":
            fields = {"status": "已签收"}
        return envelope(node.order_id, 1, fields)


def harness(spec, gateway=None):
    run = {"id": uuid.uuid4(), "invocation_id": uuid.uuid4(), "fencing": 1, "customer_id": "alice"}
    return Harness(config(), MemoryStore(), run, StubPlanner(spec), gateway or StubGateway())


@pytest.mark.asyncio
async def test_interrupt_resume_runs_planner_once_and_uses_program_dag():
    h = harness(TaskSpec(goals=[Goal.LOGISTICS], missing_inputs=["order_id"]))
    graph = h.graph(InMemorySaver())
    cfg = {"configurable": {"thread_id": "test-resume"}}
    first = await graph.ainvoke({"message": "查物流"}, cfg, durability="sync")
    assert first["__interrupt__"] and h.gateway.calls == []
    assert isinstance(h.terminal({}, first["__interrupt__"][0]), RunFinishedEvent)
    final = await graph.ainvoke(Command(resume={"order_ids": ["ORD-1001"]}), cfg, durability="sync")
    assert final["status"] == "SUCCEEDED" and h.planner.calls == 1
    assert h.gateway.calls == ["get_order", "get_logistics"]
    assert final["result"]["entries"][0]["evidence"]["source"] == "commerce_simulator"


def test_schema_repair_reports_query_scope_constraint_without_untrusted_input():
    try:
        TaskSpec(goals=[Goal.QUERY], queries=[QuerySpec(order_id="ORD-1001", query="伪造授权")])
    except ValidationError as exc:
        assert validation_reason(exc) == "query_scope_mismatch"
    else:
        pytest.fail("ungrounded scope accepted")


@pytest.mark.asyncio
@pytest.mark.parametrize("escape", [None, "foreign_order", "different_goal", "rewritten_query"])
async def test_query_clarification_preserves_subgoals_and_rejects_scope_expansion(escape):
    missing = TaskSpec(goals=[Goal.QUERY], missing_inputs=["order_id"])
    order = "ORD-2001" if escape == "foreign_order" else "ORD-1001"
    queries = [
        QuerySpec(order_id=order, query="发票类型"),
        QuerySpec(order_id=order, query="物流轨迹节点"),
    ]
    if escape == "rewritten_query":
        queries[0].query = "退款"
    resolved = TaskSpec(order_ids=[order], goals=[Goal.QUERY], queries=queries)
    if escape == "different_goal":
        resolved.goals.append(Goal.REFUND)

    class ClarifyingPlanner(StubPlanner):
        async def parse(self, _message, **_kwargs):
            self.calls += 1
            return (missing if self.calls == 1 else resolved), {"llm_calls": 1, "total_tokens": 20}

    h = harness(missing)
    h.planner = ClarifyingPlanner(missing)
    graph, cfg = h.graph(InMemorySaver()), {"configurable": {"thread_id": "query-resume"}}
    first = await graph.ainvoke({"message": "查询发票类型和物流轨迹节点"}, cfg, durability="sync")
    assert first["__interrupt__"] and not h.gateway.calls
    if escape:
        with pytest.raises(PlanningError, match="ungrounded_clarification_scope"):
            await graph.ainvoke(Command(resume={"order_ids": ["ORD-1001"]}), cfg, durability="sync")
        assert not h.gateway.calls
    else:
        final = await graph.ainvoke(
            Command(resume={"order_ids": ["ORD-1001"]}), cfg, durability="sync"
        )
        assert final["status"] == "SUCCEEDED" and final["llm_calls"] == 2
        assert [n["query"] for n in final["plan"]["nodes"] if n["tool"] == "business_query"] == [
            "发票类型",
            "物流轨迹节点",
        ]
        assert h.gateway.calls == ["get_order", "business_query", "business_query"]


@pytest.mark.asyncio
async def test_repeated_error_stops_with_no_progress_without_unbounded_model_calls():
    h = harness(
        TaskSpec(order_ids=["ORD-1001"], goals=[Goal.ORDER]), StubGateway("upstream_unavailable")
    )
    final = await h.graph(InMemorySaver()).ainvoke(
        {"message": "查询ORD-1001"}, {"configurable": {"thread_id": "loop"}}
    )
    assert final["status"] == "FAILED"
    assert final["failures"] == {"ORD-1001:get_order": "no_progress"}
    assert final["tool_calls"] == 2 and final["llm_calls"] == 2 and final["replans"] == 1


@pytest.mark.asyncio
async def test_write_intent_has_no_executable_tools_and_explicit_handoff():
    h = harness(TaskSpec(order_ids=["ORD-1001"], goals=[Goal.WRITE]))
    final = await h.graph(InMemorySaver()).ainvoke(
        {"message": "退款"}, {"configurable": {"thread_id": "write"}}
    )
    assert final["status"] == "HANDOFF" and h.gateway.calls == []
    assert "未执行任何业务写操作" in final["result"]["message"]


@pytest.mark.asyncio
async def test_tool_budget_stops_before_another_call():
    h = harness(TaskSpec(order_ids=["ORD-1001"], goals=[Goal.ORDER]))
    await h.check({"tool_calls": 30})  # Verification/finalization may consume the last result.
    with pytest.raises(PlanningError, match="tool_budget"):
        await h.execute({"tool_calls": 30})
    assert h.gateway.calls == []


@pytest.mark.asyncio
async def test_read_result_replay_does_not_call_external_tool_twice():
    h = harness(TaskSpec(order_ids=["ORD-1001"], goals=[Goal.ORDER]))
    state = {
        "message": "查ORD-1001",
        "plan": compile_plan(h.planner.spec).model_dump(mode="json"),
        "current": "ORD-1001:get_order",
        "attempts": {},
        "evidence": {},
        "tool_calls": 0,
    }
    first = await h.execute(state)
    replay = await h.execute(state)
    assert len(h.gateway.calls) == 1
    assert first["observation"] == replay["observation"]


@pytest.mark.asyncio
async def test_planner_replay_uses_durable_validated_task_instead_of_new_llm_call():
    h = harness(TaskSpec(order_ids=["ORD-1001"], goals=[Goal.ORDER]))
    first = await h.understand({"message": "查ORD-1001"})
    replay = await h.understand({"message": "查ORD-1001"})
    assert h.planner.calls == 1 and first["spec"] == replay["spec"]


def test_customer_routes_require_auth_and_production_demo_login_disabled():
    settings = config()
    settings.app_env = "production"
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/runs").status_code == 401
        assert (
            client.post(
                "/api/v1/auth/login", json={"username": "alice", "password": "dummy"}
            ).status_code
            == 503
        )
