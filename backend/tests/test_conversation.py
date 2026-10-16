import json

import pytest

from app.agent.planner import PlanningError, grounded_spec
from app.context.conversation import compact_turns, model_context, reference_ids
from app.core.config import Settings


def turns():
    return [
        {
            "seq": i,
            "run_id": str(i),
            "message": "查询订单与历史说明" * 100,
            "status": "SUCCEEDED",
            "order_ids": ["ORDER-FIRST" if i == 1 else "ORDER-LAST"],
            "amount_minor": 99999,
            "approved": True,
        }
        for i in range(1, 41)
    ]


def test_old_entity_survives_window_without_inheriting_facts_or_approval():
    packet = compact_turns(turns())
    assert reference_ids("最开始那个订单还能退多少钱", packet) == ["ORDER-FIRST"]
    assert reference_ids("刚才那个订单的物流", packet) == ["ORDER-LAST"]
    assert reference_ids("查物流", packet) == []
    context = model_context("最开始那个订单还能退多少钱", packet)
    raw = json.dumps(context, ensure_ascii=False)
    assert len(raw.encode()) < 9000
    assert "99999" not in raw and "approved" not in raw
    assert len(context["recent_turns"]) == 5


def test_ambiguous_last_entity_does_not_pick_a_random_order():
    history = turns()
    history[-1]["order_ids"] = ["ORDER-A", "ORDER-B"]
    assert reference_ids("那个订单退款", compact_turns(history)) == []
    assert reference_ids("这些订单物流", compact_turns(history)) == ["ORDER-A", "ORDER-B"]


def test_multi_reference_preserves_text_order_and_rejects_missing_or_negated_ordinal():
    history = turns()
    history[0].update(
        message="先查ORDER-FIRST，再查ORDER-SECOND", order_ids=["ORDER-SECOND", "ORDER-FIRST"]
    )
    packet = compact_turns(history)
    assert reference_ids("第一个和第二个订单的物流", packet) == ["ORDER-FIRST", "ORDER-SECOND"]
    assert reference_ids("最早那个和刚才那个订单", packet) == ["ORDER-FIRST", "ORDER-LAST"]
    assert reference_ids("不要查第一个，只查第二个", packet) == []
    assert reference_ids("第三个订单", compact_turns(history[:1])) == []


def test_reference_grounding_keeps_query_literal_and_requires_known_history():
    raw = json.dumps({"order_ids": ["ORDER-FIRST"], "goals": ["logistics"]})
    with pytest.raises(PlanningError):
        grounded_spec(raw, "那个订单物流")
    assert grounded_spec(raw, "那个订单物流", ["ORDER-FIRST"]).order_ids == ["ORDER-FIRST"]
    raw = json.dumps(
        {
            "order_ids": ["ORDER-FIRST"],
            "goals": ["business_query"],
            "queries": [{"order_id": "ORDER-FIRST", "query": "库存"}],
        }
    )
    with pytest.raises(PlanningError):
        grounded_spec(raw, "那个订单物流", ["ORDER-FIRST"])


def test_openrouter_credentials_and_routing_are_scoped_to_gateway():
    settings = Settings(
        _env_file=None,
        _secrets_dir=None,
        zhipu_api_key="official",
        openrouter_api_key="gateway",
        zhipu_api_base="https://openrouter.ai/api/v1",
    )
    assert settings.llm_api_key.get_secret_value() == "gateway"
    assert settings.llm_request_options["reasoning"] == {"enabled": False}
    assert settings.llm_request_options["provider"]["require_parameters"] is True
    assert "wafer" in settings.llm_request_options["provider"]["only"]
    settings.zhipu_api_base = "https://api.deepseek.com"
    assert settings.llm_api_key.get_secret_value() == "official"
    assert settings.llm_request_options == {"thinking": {"type": "disabled"}}
