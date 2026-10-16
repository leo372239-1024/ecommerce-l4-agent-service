"""Prospective new memory compositions after v1 became development feedback."""

import json
from pathlib import Path

from generate_confirmation import case, ev, order

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = ROOT / "docs/evaluation/step10-memory-v2.json"
    assert not target.exists(), "never overwrite a registered dataset"
    cases = []
    for length in (12, 45, 90, 175):
        paid = 7823 + length
        remaining = paid - 700
        templates = [
            (
                "查询最早那个订单的支付方式和发票开具状态，仅查询",
                [
                    ev("A", {"method": "SIMULATED_CARD"}, "get_order_payment"),
                    ev("A", {"status": "NOT_ISSUED"}, "get_order_invoice"),
                ],
            ),
            (
                "核对第二个订单的优惠分摊和当前物流，不办理售后",
                [
                    ev("B", {"discount_minor": 1000}, "get_order_discounts"),
                    ev("B", {"status": "在途"}, "get_logistics"),
                ],
            ),
            (
                "查询第三个订单所购商品的材质和商品配送费用",
                [
                    ev("C", {"material": "不锈钢"}, "get_product_specs"),
                    ev("C", {"fee_minor": 0}, "get_product_shipping"),
                ],
            ),
            (
                "查询刚才那个订单的预计送达时间和承运公司",
                [
                    ev("D", {"estimate": "2026-10-10"}, "get_delivery_estimate"),
                    ev("D", {"carrier": "演示物流"}, "get_carrier_contact"),
                ],
            ),
            (
                "最开始那个订单现在剩余可退余额是多少？只查询不退款",
                [ev("A", {"remaining_minor": remaining}, "get_refund_balance")],
            ),
            (
                "核对第二个订单退货后优惠券返还规则和人工客服服务时间",
                [
                    ev("B", {"restored_on_full_refund": True}, "get_coupon_return_policy"),
                    ev("B", {"hours": "09:00-18:00"}, "get_support_hours"),
                ],
            ),
            (
                "只查询第一个和第二个订单各自的当前订单状态",
                [
                    ev("A", {"status": "DELIVERED"}, "get_order"),
                    ev("B", {"status": "SHIPPED"}, "get_order"),
                ],
            ),
            (
                "仅查询最开始那个和刚才那个订单各自的当前物流",
                [
                    ev("A", {"status": "已签收"}, "get_logistics"),
                    ev("D", {"status": "已签收"}, "get_logistics"),
                ],
            ),
            (
                "分别查询第二个订单的当前物流以及第三个订单的订单状态",
                [
                    ev("B", {"status": "在途"}, "get_logistics"),
                    ev("C", {"status": "PAID"}, "get_order"),
                ],
            ),
            (
                "请查询最早那个订单的退货验收状态和原路退款到账时效",
                [
                    ev("A", {"status": "ACCEPTED"}, "get_return_status"),
                    ev("A", {"max_business_days": 3}, "get_refund_eta"),
                ],
            ),
            (
                "查询第三个订单的商品保修范围以及脱敏收货城市，不改地址",
                [
                    ev("C", {"days": 365}, "get_product_warranty"),
                    ev("C", {"city": "杭州市"}, "get_order_address_masked"),
                ],
            ),
            (
                "查询同一笔订单的支付状态和可退金额试算，不提交退款",
                [
                    ev("D", {"status": "PAID"}, "get_order_payment"),
                    ev("D", {"amount_minor": 3000}, "quote_refund"),
                ],
            ),
        ]
        for index, (message, evidence) in enumerate(templates):
            c = case(
                f"MV{len(cases) + 1:03}",
                f"memory_v2_composition_{index}",
                message,
                {
                    "A": order(paid, refunded=700),
                    "B": order(6150, "SHIPPED"),
                    "C": order(9200, "PAID"),
                    "D": order(3400, refunded=400),
                },
                evidence,
                difficulty="long",
            )
            c["history_fixture"] = {
                "turns": length,
                "kind": "synthetic_frozen_authorized_historical_snapshots",
                "stale_amount_minor": 99999,
                "prior_approval_is_not_authorization": True,
            }
            c["scripted_user"]["missing_order"] = None
            c["customer_id"] = "customer-bob"
            for o in c["fixture"]["orders"].values():
                o["owner"] = "customer-bob"
            cases.append(c)
    for index in range(12):
        c = case(
            f"MV{len(cases) + 1:03}",
            "memory_v2_explicit_control",
            "查询{A}的订单状态和当前物流，仅查询，不办理售后。",
            {
                "A": order(8300 + index),
                "B": order(6150, "SHIPPED"),
                "C": order(9200, "PAID"),
                "D": order(3400),
            },
            [
                ev("A", {"status": "DELIVERED"}, "get_order"),
                ev("A", {"status": "已签收"}, "get_logistics"),
            ],
            difficulty="long",
        )
        c["history_fixture"] = {
            "turns": (12, 45, 90, 175)[index % 4],
            "kind": "synthetic_frozen_authorized_historical_snapshots",
        }
        c["scripted_user"]["missing_order"] = None
        c["customer_id"] = "customer-bob"
        for o in c["fixture"]["orders"].values():
            o["owner"] = "customer-bob"
        cases.append(c)
    assert len(cases) == 60
    target.write_text(
        json.dumps(
            {
                "version": "step10-memory-v2",
                "review_status": "prospectively registered new project-authored compositions after v1 development feedback; no independent human blind review",
                "cases": cases,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"cases": len(cases), "families": len({c["family"] for c in cases})}))


if __name__ == "__main__":
    main()
