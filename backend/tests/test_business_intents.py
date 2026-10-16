import json

import pytest

from app.agent.contracts import compile_plan
from app.agent.planner import grounded_spec
from app.business.intents import exact_effect, split_query


def test_independent_projections_compile_into_complete_grounded_nodes():
    message = "查询SHOP-ab12-A物流轨迹节点和预计送达时间以及退款余额和积分扣回数量"
    raw = {
        "order_ids": ["SHOP-ab12-A"],
        "goals": ["business_query"],
        "queries": [
            {
                "order_id": "SHOP-ab12-A",
                "query": "物流轨迹节点和预计送达时间以及退款余额和积分扣回数量",
            }
        ],
    }
    spec = grounded_spec(json.dumps(raw), message)
    queries = [n.query for n in compile_plan(spec).nodes if n.tool == "business_query"]
    assert queries == ["物流轨迹节点", "预计送达时间", "退款余额", "积分扣回数量"]
    assert all(query in message for query in queries)


def test_same_projection_and_negated_requests_are_not_blindly_split():
    assert split_query("商品材质和容量") == ["商品材质和容量"]
    assert split_query("不要查库存只查当前售价") == ["不要查库存只查当前售价"]
    assert exact_effect("不要查库存只查当前售价") is None
    assert exact_effect("改地址") is None
    assert exact_effect("库存数量") == "get_product_inventory"
    assert split_query("换货库存和发票类型") == ["换货库存", "发票类型"]


@pytest.mark.parametrize("restriction", ["仅查询", "只查询", "只查"])
def test_read_only_restriction_keeps_all_explicitly_requested_facets(restriction):
    message = f"查询ORD-1002的物流轨迹节点和预计送达时间，{restriction}。"
    raw = {
        "order_ids": ["ORD-1002"],
        "goals": ["business_query"],
        "queries": [{"order_id": "ORD-1002", "query": message}],
    }
    spec = grounded_spec(json.dumps(raw), message)
    plan = compile_plan(spec)
    assert [n.query for n in plan.nodes if n.tool == "business_query"] == [
        "物流轨迹节点",
        "预计送达时间",
    ]
    assert all(n.risk == "read" for n in plan.nodes)
