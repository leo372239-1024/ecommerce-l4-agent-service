import json

import pytest

from app.agent.contracts import compile_plan
from app.agent.planner import PlanningError, grounded_spec
from app.business.catalog import CATALOG
from app.business.catalog_view import bound_projection
from app.business.intents import exact_effect


def specification(query, **extra):
    return {
        "order_ids": ["ORD-1002"],
        "goals": ["business_query"],
        "queries": [{"order_id": "ORD-1002", "query": query}],
        **extra,
    }


@pytest.mark.parametrize("query", ["承运公司", "承运公司名称", "承运商", "物流公司"])
def test_carrier_identity_has_an_owned_read_projection(query):
    parsed = grounded_spec(json.dumps(specification(query)), "查询ORD-1002的" + query)
    assert bound_projection(parsed.queries[0].query, "ORD-1002", CATALOG.values()) == (
        "get_carrier_contact",
        {"order_id": "ORD-1002"},
    )
    assert all(node.risk == "read" for node in compile_plan(parsed).nodes)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("预计送达时间和承运公司", {"get_delivery_estimate", "get_carrier_contact"}),
        ("人工客服工作时间及优惠券使用门槛", {"get_support_hours", "get_coupon_rules"}),
    ],
)
def test_compound_queries_cannot_finish_with_only_the_first_known_projection(query, expected):
    parsed = grounded_spec(json.dumps(specification(query)), "查询ORD-1002的" + query)
    nodes = [node for node in compile_plan(parsed).nodes if node.tool == "business_query"]
    assert {exact_effect(node.query) for node in nodes} == expected
    assert len(nodes) == 2
    assert all(node.dependencies == ["ORD-1002:get_order"] for node in nodes)


def test_model_omitted_known_projection_is_rejected_before_execution():
    with pytest.raises(PlanningError, match="^requested_projection_omitted$"):
        grounded_spec(json.dumps(specification("支付方式")), "查询ORD-1002支付方式和发票类型")


def test_mixed_general_knowledge_and_order_query_keeps_its_distinct_scope():
    data = specification(
        "发票类型",
        goals=["business_query", "knowledge"],
        order_goals=[{"order_id": "ORD-1002", "goals": ["business_query"]}],
        queries=[
            {"order_id": "ORD-1002", "query": "发票类型"},
            {"order_id": "", "query": "保修范围"},
        ],
    )
    parsed = grounded_spec(
        json.dumps(data), "查询ORD-1002的发票类型，并在知识库查询一般保修范围说明"
    )
    assert [(q.order_id, q.query) for q in parsed.queries] == [
        ("ORD-1002", "发票类型"),
        ("", "保修范围"),
    ]


def test_negated_inventory_request_does_not_add_an_inventory_goal():
    parsed = grounded_spec(
        json.dumps(specification("当前售价")), "不要查ORD-1002的库存，只查当前售价"
    )
    assert [q.query for q in parsed.queries] == ["当前售价"]
    assert exact_effect(parsed.queries[0].query) == "get_product_price"


@pytest.mark.parametrize("query", ["承运公司赔付政策", "物流公司如何处理投诉"])
def test_other_carrier_questions_are_not_forced_into_contact_projection(query):
    assert exact_effect(query) is None
