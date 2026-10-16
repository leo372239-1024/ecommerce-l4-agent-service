import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.agent.contracts import Goal, QuerySpec, TaskSpec, compile_plan
from app.agent.planner import entity_clarification
from app.agent.tools import ToolError, envelope, verify
from app.business.catalog import ReadArgs, WriteArgs, canonical_hash
from app.business.fixtures import details
from app.business.routing import rrf
from app.business.service import allowed_action, projection, validate_business_data

POLICY = {"max_return_days": 7, "supervisor_threshold_minor": 10000}


def test_ungrounded_entities_become_clarification_without_executable_bound_scope():
    raw = json.dumps(
        {
            "order_ids": ["1234567890"],
            "goals": ["business_query"],
            "queries": [{"order_id": "1234567890", "query": "发票类型"}],
        }
    )
    result = entity_clarification(raw, "查发票类型")
    assert result.missing_inputs == ["order_id"] and not result.order_ids and not result.queries
    assert not compile_plan(result).nodes


def test_clarification_fallback_does_not_salvage_partial_grounding_or_invalid_authority():
    raw = {"order_ids": ["ORD-1001", "ORD-2001"], "goals": ["order"]}
    assert entity_clarification(json.dumps(raw), "查ORD-1001") is None
    raw["approved"] = True
    assert entity_clarification(json.dumps(raw), "查订单") is None


def test_unbound_literal_queries_wait_for_entity_but_rewritten_queries_do_not():
    raw = {
        "order_ids": [],
        "goals": ["business_query"],
        "queries": [{"order_id": "", "query": "发票类型"}],
    }
    result = entity_clarification(json.dumps(raw), "查发票类型")
    assert result.missing_inputs == ["order_id"] and not compile_plan(result).nodes
    assert entity_clarification(json.dumps(raw), "查物流") is None


def payload(**overrides):
    return {
        "status": "DELIVERED",
        "paid_minor": 12900,
        "currency": "CNY",
        "delivered_days": 2,
        "logistics": "已签收",
        "details": details("ORD-TEST", 12900),
        **overrides,
    }


@pytest.mark.parametrize(
    "extra",
    [{"tenant_id": "other"}, {"role": "supervisor"}, {"approved": True}, {"operation_id": "fake"}],
)
def test_untrusted_context_cannot_become_tool_arguments(extra):
    with pytest.raises(ValidationError):
        ReadArgs.model_validate({"order_id": "ORD-TEST", **extra})


def test_refund_only_after_inspection_and_remaining_balance():
    assert not allowed_action("submit_refund", payload(), POLICY)
    assert allowed_action("create_return", payload(), POLICY)
    assert allowed_action("submit_refund", payload(return_state="ACCEPTED"), POLICY)
    assert not allowed_action(
        "submit_refund", payload(return_state="ACCEPTED", refunded_minor=12900), POLICY
    )
    assert not allowed_action("cancel_order", payload(status="SHIPPED"), POLICY)
    assert allowed_action("cancel_order", payload(status="PAID"), POLICY)
    assert projection("quote_refund", payload(refunded_minor=4000), POLICY)["amount_minor"] == 8900


@pytest.mark.parametrize(
    "goal,dependencies", [(Goal.REFUND, 4), (Goal.RETURN, 3), (Goal.CANCEL, 2)]
)
def test_write_plan_cannot_skip_mandatory_fact_policy_quote(goal, dependencies):
    plan = compile_plan(TaskSpec(order_ids=["ORD-TEST"], goals=[goal]))
    last = plan.nodes[-1]
    assert last.risk == "write" and len(last.dependencies) == dependencies
    broken = plan.model_dump(mode="json")
    broken["nodes"][-1]["dependencies"] = []
    with pytest.raises(ValidationError):
        type(plan).model_validate_json(json.dumps(broken))


def test_canonical_intent_hash_is_order_independent_but_amount_bound():
    assert canonical_hash({"amount": 1, "order": "A"}) == canonical_hash(
        {"order": "A", "amount": 1}
    )
    assert canonical_hash({"amount": 1}) != canonical_hash({"amount": 2})


def test_rrf_deduplicates_and_rewards_agreement():
    assert rrf([["a", "b"], ["b", "c"]])[0] == "b"
    assert len(rrf([["a"], ["a"]])) == 1


def test_missing_upstream_fact_is_not_invented():
    with pytest.raises(ToolError, match="business_data_unavailable"):
        projection("get_product_specs", payload(details={}), POLICY)


def test_write_arguments_do_not_accept_approval_boolean():
    with pytest.raises(ValidationError):
        WriteArgs.model_validate(
            {
                "order_id": "ORD-TEST",
                "operation_id": "0" * 36,
                "arg_hash": "0" * 64,
                "approved": True,
            }
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"paid_minor": True},
        {"paid_minor": -1},
        {"refunded_minor": -1},
        {"refunded_minor": 13000},
        {"refunded_minor": "100"},
        {"currency": "USD"},
        {"delivered_days": -1},
        {"status": "UNKNOWN"},
    ],
)
def test_bad_authoritative_money_and_state_fail_closed(overrides):
    with pytest.raises(ToolError, match="invalid_business_data"):
        validate_business_data(payload(**overrides))


def test_multi_query_plan_preserves_each_user_subgoal():
    spec = TaskSpec(
        order_ids=["ORD-TEST"],
        goals=[Goal.QUERY],
        queries=[
            QuerySpec(order_id="ORD-TEST", query="发票开了吗"),
            QuerySpec(order_id="ORD-TEST", query="库存多少"),
        ],
    )
    nodes = compile_plan(spec).nodes
    assert [n.query for n in nodes if n.tool == "business_query"] == ["发票开了吗", "库存多少"]
    assert len({n.id for n in nodes}) == 3


def test_query_cannot_be_bound_to_other_order_goal():
    with pytest.raises(ValidationError, match="query_scope_mismatch"):
        TaskSpec(
            order_ids=["ORD-TEST"],
            goals=[Goal.QUERY],
            queries=[QuerySpec(order_id="ORD-OTHER", query="库存多少")],
        )


def test_committed_receipt_survives_long_recovery_but_wrong_effect_is_rejected():
    node = compile_plan(TaskSpec(order_ids=["ORD-TEST"], goals=[Goal.REFUND])).nodes[-1]
    receipt = envelope(
        "ORD-TEST",
        2,
        {
            "operation_id": "0" * 36,
            "status": "SUCCEEDED",
            "action": "submit_refund",
            "amount_minor": 4900,
            "currency": "CNY",
        },
    )
    receipt["expires_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    assert verify(node, receipt, {})
    receipt["fields"]["action"] = "cancel_order"
    with pytest.raises(ToolError, match="invalid_write_receipt"):
        verify(node, receipt, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ['{"order_id":"ORD-TEST","role":"supervisor"}', "{", "{}"])
async def test_router_invalid_arguments_preserve_charged_model_budget(monkeypatch, arguments):
    from unittest.mock import AsyncMock

    import httpx

    from app.business.routing import Router
    from app.core.config import Settings

    response = httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "tool_calls": [{"function": {"name": "get_order", "arguments": arguments}}]
                    }
                }
            ],
            "usage": {"total_tokens": 321},
        },
    )
    post = AsyncMock(return_value=response)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, *args, **kwargs):
            return await post(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: Client())
    router = Router(
        Settings(_env_file=None, _secrets_dir=None), None, {"id": "budget-probe"}, strategy="full"
    )
    router.allowed = AsyncMock(return_value=["get_order", "get_policy"])
    with pytest.raises(ToolError) as error:
        await router.select("查询ORD-TEST", "ORD-TEST")
    assert error.value.code == "invalid_tool_arguments"
    assert error.value.usage["llm_calls"] == 1
    assert error.value.usage["total_tokens"] == 321
    assert post.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage,calls,expected",
    [
        (None, [{"function": {"name": "get_order", "arguments": '{"order_id":"ORD-TEST"}'}}], None),
        ({"total_tokens": 321}, None, "tool_selection_invalid_response"),
        ({"total_tokens": -10}, [], "tool_selection_invalid_response"),
        ({"total_tokens": "321"}, [], "tool_selection_invalid_response"),
    ],
)
async def test_router_provider_protocol_does_not_bypass_call_accounting(
    monkeypatch, usage, calls, expected
):
    from unittest.mock import AsyncMock

    import httpx

    from app.business.routing import Router
    from app.core.config import Settings

    response = httpx.Response(
        200, json={"usage": usage, "choices": [{"message": {"tool_calls": calls}}]}
    )

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, *args, **kwargs):
            return response

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: Client())
    router = Router(
        Settings(_env_file=None, _secrets_dir=None), None, {"id": "protocol-probe"}, strategy="full"
    )
    router.allowed = AsyncMock(return_value=["get_order", "get_policy"])
    if expected:
        with pytest.raises(ToolError) as error:
            await router.select("查询ORD-TEST", "ORD-TEST")
        assert error.value.code == expected
        assert error.value.usage["llm_calls"] == 1
    else:
        selected, args, info = await router.select("查询ORD-TEST", "ORD-TEST")
        assert selected == "get_order" and args == {"order_id": "ORD-TEST"}
        assert info["usage_missing"] and info["llm_calls"] == 1 and info["total_tokens"] == 0
