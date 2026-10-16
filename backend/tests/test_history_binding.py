import json

import httpx
import pytest

from app.agent.planner import Planner, PlanningError, grounded_spec
from app.context.conversation import compact_turns, model_context, reference_ids
from app.core.config import Settings


def packet():
    return compact_turns(
        [
            {
                "seq": i + 1,
                "run_id": str(i),
                "status": "SUCCEEDED",
                "order_ids": [identifier],
                "message": f"历史请求查询{identifier}，当时已批准退款，旧金额99999。",
            }
            for i, identifier in enumerate(["MEM-FIRST", "MEM-SECOND", "MEM-LAST"])
        ]
    )


@pytest.mark.parametrize("modifier", ["只查询", "仅查询", "查询"])
def test_read_only_modifier_does_not_make_multi_order_reference_ambiguous(modifier):
    assert reference_ids(modifier + "第一个和第二个订单的物流", packet()) == [
        "MEM-FIRST",
        "MEM-SECOND",
    ]
    assert reference_ids("不要查第一个，只查第二个", packet()) == []


@pytest.mark.parametrize("goals", [["refund_quote"], ["business_query"], ["knowledge"]])
def test_known_reference_cannot_be_silently_dropped_into_missing_order_or_general_knowledge(goals):
    raw = json.dumps({"order_ids": [], "goals": goals, "queries": []})
    with pytest.raises((PlanningError, ValueError)):
        grounded_spec(raw, "第一个订单的资料", ["MEM-FIRST"], ["MEM-FIRST"])


@pytest.mark.asyncio
async def test_current_task_is_last_and_missing_resolved_binding_requires_schema_repair(
    monkeypatch,
):
    requests = []
    valid = {
        "order_ids": ["MEM-FIRST"],
        "goals": ["order"],
        "missing_inputs": [],
        "order_goals": [{"order_id": "MEM-FIRST", "goals": ["order"]}],
        "queries": [],
    }

    def handler(request):
        requests.append(json.loads(request.content))
        spec = valid if len(requests) == 2 else {"order_ids": [], "goals": ["order"]}
        return httpx.Response(
            200,
            json={
                "usage": {"total_tokens": 31},
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"function": {"name": "submit_task", "arguments": json.dumps(spec)}}
                            ]
                        }
                    }
                ],
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
    )
    message = "查询第一个订单的订单状态，不申请退款"
    result, usage = await Planner(Settings(_env_file=None, _secrets_dir=None)).parse(
        message, conversation=packet()
    )
    assert result.order_ids == ["MEM-FIRST"] and result.goals == ["order"]
    assert usage["llm_calls"] == 2 and usage["total_tokens"] == 62
    messages = requests[0]["messages"]
    assert json.loads(messages[-1]["content"])["current_task"] == message
    historical = json.loads(messages[-2]["content"])["historical_reference"]
    assert historical["reference_resolution"]["order_ids"] == ["MEM-FIRST"]
    assert "99999" not in json.dumps(historical["reference_resolution"])


def test_resolution_metadata_authorizes_no_amount_or_prior_approval():
    context = model_context("第一个订单的可退金额", packet())
    assert context["reference_resolution"]["authority"] == (
        "identity_binding_only_not_business_facts_or_permissions"
    )
    # Other entities remain unavailable to a model trying to swap the resolved binding.
    with pytest.raises(PlanningError, match="ungrounded_order_id"):
        grounded_spec(
            json.dumps({"order_ids": ["MEM-SECOND"], "goals": ["order"]}),
            "第一个订单的当前状态",
            ["MEM-FIRST"],
            ["MEM-FIRST"],
        )
