"""Author deterministic confirmation fixtures before inference; never adapt to candidate scores."""

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def order(paid, status="DELIVERED", days=2, return_state="ACCEPTED", refunded=0):
    return {
        "owner": "customer-alice",
        "payload": {
            "status": status,
            "paid_minor": paid,
            "refunded_minor": refunded,
            "currency": "CNY",
            "delivered_days": days,
            "logistics": "在途"
            if status == "SHIPPED"
            else "未发货"
            if status == "PAID"
            else "已签收",
            "return_state": return_state,
        },
    }


def case(
    identifier,
    family,
    message,
    orders,
    evidence,
    effects=(),
    statuses=("SUCCEEDED",),
    intents=(),
    approval="confirm_and_approve",
    difficulty="complex",
):
    return {
        "id": identifier,
        "family": family,
        "partition": "evaluation",
        "seed": 10000 + int(identifier[2:]),
        "message": message,
        "fixture": {"orders": orders},
        "scripted_user": {
            "approval": approval,
            "allowed_write_intents": list(intents),
            "missing_order": "A",
        },
        "oracle": {
            "statuses": list(statuses),
            "evidence": evidence,
            "effects": list(effects),
            "forbidden": [],
        },
        "difficulty": difficulty,
        "provenance": "project-authored new task compositions and authoritative simulator state; frozen before confirmation inference",
        "human_review": "pending",
    }


def ev(alias, fields, tool):
    return {"order": alias, "fields": fields, "evidence_tools": [tool]}


def generate():
    # Each pair requires distinct projections, not two paraphrases of the same tool.
    facets = [
        ("发票类型", "get_order_invoice", {"type": "电子普通发票"}),
        (
            "物流轨迹节点",
            "get_shipment_tracking",
            {"nodes": [{"status": "商家交运", "timestamp": "2026-10-02T02:00:00Z"}]},
        ),
        ("预计送达时间", "get_delivery_estimate", {"estimate": "2026-10-10"}),
        ("商品材质和容量", "get_product_specs", {"material": "不锈钢", "capacity_ml": 500}),
        ("库存数量", "get_product_inventory", {"available_quantity": 15}),
        ("保修年限和保修范围", "get_product_warranty", {"days": 365}),
        ("是否还在保修期", "get_warranty_eligibility", {"eligible": True}),
        ("剩余可退余额", "get_refund_balance", {"remaining_minor": 7300}),
        ("积分扣回数量", "get_loyalty_impact", {"points_to_reverse": 73}),
        ("人工客服服务时间", "get_support_hours", {"hours": "09:00-18:00"}),
        ("退货验收状态", "get_return_status", {"status": "ACCEPTED"}),
        ("退款到账时效", "get_refund_eta", {"max_business_days": 3}),
        ("退货仓地址", "get_return_instructions", {"warehouse": "演示退货仓"}),
        ("退货运费承担和金额", "get_return_shipping_fee", {"payer": "商家", "fee_minor": 800}),
        ("优惠券返还规则", "get_coupon_return_policy", {"restored_on_full_refund": True}),
        ("支付方式", "get_order_payment", {"method": "SIMULATED_CARD"}),
        (
            "商品配送区域和商品运费",
            "get_product_shipping",
            {"regions": ["中国大陆"], "fee_minor": 0},
        ),
        ("脱敏收货地址", "get_order_address_masked", {"city": "杭州市"}),
        ("包裹重量", "get_shipment_packages", {"packages": [{"weight_grams": 400}]}),
        ("工单状态", "get_ticket_status", {"tickets": []}),
    ]
    cases = []
    pair_indices = [(i, (i + 1) % 20) for i in range(20)] + [(0, 10), (2, 15), (3, 18), (7, 14)]
    for i, (left, right) in enumerate(pair_indices, 1):
        a, b = facets[left], facets[right]
        message = f"请分别核对{{A}}的{a[0]}与{b[0]}，然后查询{{B}}的订单状态和当前物流；不要替{{B}}办理售后。"
        evidence = [
            ev("A", a[2], a[1]),
            ev("A", b[2], b[1]),
            ev("B", {"status": "SHIPPED"}, "get_order"),
            ev("B", {"status": "在途"}, "get_logistics"),
        ]
        cases.append(
            case(
                f"CP{i:03}",
                f"complex_projection_{a[1]}_{b[1]}",
                message,
                {"A": order(7300), "B": order(8600, "SHIPPED")},
                evidence,
            )
        )
    # Distinct policy/approval/ownership decision structures with genuine business effects.
    for j in range(12):
        i = len(cases) + 1
        refund = 4100 + j * 733
        cancel = 3200 + j * 431
        return_only = j % 3 == 1
        first_action = "create_return" if return_only else "submit_refund"
        action_text = "申请退货" if return_only else "办理整单退款"
        first_amount = 0 if return_only else refund
        a = order(refund, return_state="NONE" if return_only else "ACCEPTED")
        message = f"先核对{{C}}的商品材质和库存数量，为{{A}}{action_text}，再取消尚未发货的{{B}}；{{C}}只查询，不办理退款或取消。"
        effects = [
            {"order": "A", "action": first_action, "amount_minor": first_amount},
            {"order": "B", "action": "cancel_order", "amount_minor": cancel},
        ]
        cases.append(
            case(
                f"CP{i:03}",
                f"complex_write_{first_action}_{'supervisor' if max(refund, cancel) > 5000 else 'customer'}_{j % 3}",
                message,
                {"A": a, "B": order(cancel, "PAID"), "C": order(7300)},
                [
                    ev("C", {"material": "不锈钢"}, "get_product_specs"),
                    ev("C", {"available_quantity": 15}, "get_product_inventory"),
                ],
                effects,
                intents=[
                    {"order": "A", "action": first_action},
                    {"order": "B", "action": "cancel_order"},
                ],
            )
        )
    for facet in facets:
        i = len(cases) + 1
        cases.append(
            case(
                f"CP{i:03}",
                "single_" + facet[1],
                "麻烦帮我核实订单{A}的" + facet[0] + "，仅查询信息。",
                {"A": order(7300)},
                [ev("A", facet[2], facet[1])],
                difficulty="normal",
            )
        )
    # Keep development cases separate. New IDs/context, no averaging in the old results.
    development = json.loads(
        (ROOT / "docs/evaluation/step9-dataset.json").read_text(encoding="utf-8")
    )
    edges = [
        c
        for c in development["cases"]
        if c["id"]
        in {
            "EC043",
            "EC044",
            "EC045",
            "EC046",
            "EC047",
            "EC048",
            "EC049",
            "EC050",
            "EC051",
            "EC052",
            "EC053",
            "EC054",
            "EC055",
            "EC056",
            "EC057",
            "EC058",
            "EC059",
            "EC060",
        }
    ]
    for original in edges:
        c = copy.deepcopy(original)
        c["id"] = f"CP{len(cases) + 1:03}"
        c["family"] = "edge_" + original["family"]
        c["provenance"] = "Known development edge regression; excluded from novel-only inference"
        c["novel_confirmation"] = False
        cases.append(c)
    for c in cases:
        c.setdefault("novel_confirmation", True)
    result = {
        "version": "step10-confirmation-v1",
        "policy": "simulator-v1-seven-day-5000-minor",
        "review_status": "machine-reviewed compositions; human review pending",
        "split": {
            "development": "EC001-EC060",
            "new_confirmation": "CP001-CP056",
            "known_edge_regression": "CP057-CP074",
        },
        "cases": cases,
    }
    path = ROOT / "docs/evaluation/step10-confirmation.json"
    if path.exists():
        raise RuntimeError("confirmation_already_frozen_use_versioned_filename")
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "cases": len(cases),
                "complex": sum(
                    c["difficulty"] == "complex" and c["novel_confirmation"] for c in cases
                ),
                "new": sum(c["novel_confirmation"] for c in cases),
                "file": str(path),
            }
        )
    )


if __name__ == "__main__":
    generate()
