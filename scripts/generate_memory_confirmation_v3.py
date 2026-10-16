"""New final-version task compositions; never replace any preceding registered dataset."""

import hashlib
import json
from pathlib import Path

from generate_confirmation import case, ev, order

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = ROOT / "docs/evaluation/step10-memory-v3.json"
    assert not target.exists(), "never overwrite a registered dataset"
    cases = []
    for length in (20, 50, 100, 180):
        paid = 8697 + length
        templates = [
            (
                "核对第一次提到的订单当前支付方式和退款余额，只查资料",
                [
                    ev("A", {"method": "SIMULATED_CARD"}, "get_order_payment"),
                    ev("A", {"remaining_minor": paid - 1200}, "get_refund_balance"),
                ],
            ),
            (
                "查询第二个订单预计送达时间及发票类型，不申请退款",
                [
                    ev("B", {"estimate": "2026-10-10"}, "get_delivery_estimate"),
                    ev("B", {"type": "电子普通发票"}, "get_order_invoice"),
                ],
            ),
            (
                "查看第三个订单所购商品当前库存数量和当前售价",
                [
                    ev("C", {"available_quantity": 15}, "get_product_inventory"),
                    ev("C", {"current_price_minor": 13900}, "get_product_price"),
                ],
            ),
            (
                "仅查询刚才那笔订单的退货运费承担方及原路退款到账时效",
                [
                    ev("D", {"payer": "商家"}, "get_return_shipping_fee"),
                    ev("D", {"max_business_days": 3}, "get_refund_eta"),
                ],
            ),
            (
                "请核对第一个和第二个订单各自的支付方式，只查询",
                [
                    ev("A", {"method": "SIMULATED_CARD"}, "get_order_payment"),
                    ev("B", {"method": "SIMULATED_CARD"}, "get_order_payment"),
                ],
            ),
            (
                "仅查询第二个和第三个订单各自的商品材质，不办理售后",
                [
                    ev("B", {"material": "不锈钢"}, "get_product_specs"),
                    ev("C", {"material": "不锈钢"}, "get_product_specs"),
                ],
            ),
            (
                "查询最早那个和刚才那个订单分别的发票开具状态",
                [
                    ev("A", {"status": "NOT_ISSUED"}, "get_order_invoice"),
                    ev("D", {"status": "NOT_ISSUED"}, "get_order_invoice"),
                ],
            ),
            (
                "请查第一个订单的当前物流，以及第二个订单的支付状态",
                [
                    ev("A", {"status": "已签收"}, "get_logistics"),
                    ev("B", {"status": "PAID"}, "get_order_payment"),
                ],
            ),
            (
                "只查最开始那个订单剩余可退余额，同时查第三个订单的发票类型",
                [
                    ev("A", {"remaining_minor": paid - 1200}, "get_refund_balance"),
                    ev("C", {"type": "电子普通发票"}, "get_order_invoice"),
                ],
            ),
            (
                "查询第三个订单对应商品的保修年限与库存数量",
                [
                    ev("C", {"days": 365}, "get_product_warranty"),
                    ev("C", {"available_quantity": 15}, "get_product_inventory"),
                ],
            ),
            (
                "查询同一笔订单的人工客服工作时间及优惠券使用门槛",
                [
                    ev("D", {"hours": "09:00-18:00"}, "get_support_hours"),
                    ev("D", {"min_spend_minor": 1000}, "get_coupon_rules"),
                ],
            ),
            (
                "核对最早提到那个订单的退货寄回操作说明和寄回运费，只查询",
                [
                    ev("A", {"warehouse": "演示退货仓"}, "get_return_instructions"),
                    ev("A", {"fee_minor": 800}, "get_return_shipping_fee"),
                ],
            ),
        ]
        for index, (message, evidence) in enumerate(templates):
            c = case(
                f"MF{len(cases) + 1:03}",
                f"memory_v3_composition_{index}",
                message,
                {
                    "A": order(paid, refunded=1200),
                    "B": order(7320, "SHIPPED"),
                    "C": order(10500, "PAID"),
                    "D": order(4800, refunded=700),
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
            f"MF{len(cases) + 1:03}",
            "memory_v3_explicit_control",
            "只查询{A}的发票开具状态和支付方式，不办理业务变更。",
            {
                "A": order(9500 + index),
                "B": order(7320, "SHIPPED"),
                "C": order(10500, "PAID"),
                "D": order(4800),
            },
            [
                ev("A", {"status": "NOT_ISSUED"}, "get_order_invoice"),
                ev("A", {"method": "SIMULATED_CARD"}, "get_order_payment"),
            ],
            difficulty="long",
        )
        c["history_fixture"] = {
            "turns": (20, 50, 100, 180)[index % 4],
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
                "version": "step10-memory-final-v3",
                "review_status": "new project-authored compositions registered before first final-version inference; no independent human blind review; shared-read-projection concept informed by exposed browser regression",
                "cases": cases,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "cases": len(cases),
                "families": len({c["family"] for c in cases}),
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
