"""Prospective new compositions plus separately labelled exposed carrier regressions."""

import hashlib
import json
from pathlib import Path

from generate_confirmation import case, ev, order

ROOT = Path(__file__).resolve().parents[1]


def owned_case(identifier, family, message, evidence, length, paid, known=False):
    result = case(
        identifier,
        family,
        message,
        {
            "A": order(paid, refunded=1300),
            "B": order(7820, "SHIPPED"),
            "C": order(6810, "PAID"),
            "D": order(4100, refunded=900),
        },
        evidence,
        difficulty="long",
    )
    result["history_fixture"] = {
        "turns": length,
        "kind": "synthetic_frozen_authorized_historical_snapshots",
        "stale_amount_minor": 99999,
        "prior_approval_is_not_authorization": True,
    }
    result["scripted_user"]["missing_order"] = None
    result["customer_id"] = "customer-bob"
    result["known_regression"] = known
    result["provenance"] = (
        "Completed V2 exposed carrier omission; known regression, excluded from new-composition primary metric"
        if known
        else "New compositions registered before V4 inference; carrier/coupon terminology informed by completed V2/code regression, not independently blind reviewed"
    )
    for item in result["fixture"]["orders"].values():
        item["owner"] = "customer-bob"
    return result


def main():
    output = ROOT / "docs/evaluation/step10-memory-v4.json"
    assert not output.exists(), "never replace registered data"
    cases = []
    for length in (20, 60, 110, 190):
        paid = 10403 + length
        templates = [
            (
                "查询最早那个订单的支付状态和发票类型，只查询",
                [
                    ev("A", {"status": "PAID"}, "get_order_payment"),
                    ev("A", {"type": "电子普通发票"}, "get_order_invoice"),
                ],
            ),
            (
                "核对第二个订单的优惠分摊金额与商品容量",
                [
                    ev("B", {"discount_minor": 1000}, "get_order_discounts"),
                    ev("B", {"capacity_ml": 500}, "get_product_specs"),
                ],
            ),
            (
                "查看第三个订单对应商品的质保范围及配送费用",
                [
                    ev("C", {"coverage": "非人为产品质量问题"}, "get_product_warranty"),
                    ev("C", {"fee_minor": 0}, "get_product_shipping"),
                ],
            ),
            (
                "只查询刚才那个订单退货验收状态和退款到账时效",
                [
                    ev("D", {"status": "ACCEPTED"}, "get_return_status"),
                    ev("D", {"max_business_days": 3}, "get_refund_eta"),
                ],
            ),
            (
                "第一个和第二个订单各自的保修年限是多少，仅查询",
                [
                    ev("A", {"days": 365}, "get_product_warranty"),
                    ev("B", {"days": 365}, "get_product_warranty"),
                ],
            ),
            (
                "查询第二个和第三个订单各自的优惠券使用门槛",
                [
                    ev("B", {"min_spend_minor": 1000}, "get_coupon_rules"),
                    ev("C", {"min_spend_minor": 1000}, "get_coupon_rules"),
                ],
            ),
            (
                "核对最早那个和刚才那个订单各自的人工客服工作时间",
                [
                    ev("A", {"hours": "09:00-18:00"}, "get_support_hours"),
                    ev("D", {"hours": "09:00-18:00"}, "get_support_hours"),
                ],
            ),
            (
                "查询第一个订单的当前物流，同时查询第二个订单的优惠券退回政策",
                [
                    ev("A", {"status": "已签收"}, "get_logistics"),
                    ev("B", {"restored_on_full_refund": True}, "get_coupon_return_policy"),
                ],
            ),
            (
                "查询最开始那个订单的退款余额，以及第三个订单的支付方式",
                [
                    ev("A", {"remaining_minor": paid - 1300}, "get_refund_balance"),
                    ev("C", {"method": "SIMULATED_CARD"}, "get_order_payment"),
                ],
            ),
            (
                "查看第三个订单商品库存数量与商品配送费用",
                [
                    ev("C", {"available_quantity": 15}, "get_product_inventory"),
                    ev("C", {"fee_minor": 0}, "get_product_shipping"),
                ],
            ),
            (
                "查询同一笔订单的承运公司名称和优惠金额，仅查询",
                [
                    ev("D", {"carrier": "演示物流"}, "get_carrier_contact"),
                    ev("D", {"discount_minor": 1000}, "get_order_discounts"),
                ],
            ),
            (
                "核对第一次提到的订单的寄回运费金额及退货仓地址",
                [
                    ev("A", {"fee_minor": 800}, "get_return_shipping_fee"),
                    ev("A", {"warehouse": "演示退货仓"}, "get_return_instructions"),
                ],
            ),
        ]
        for index, (message, evidence) in enumerate(templates):
            cases.append(
                owned_case(
                    f"MQ{len(cases) + 1:03}",
                    f"memory_v4_composition_{index}",
                    message,
                    evidence,
                    length,
                    paid,
                )
            )
    for index in range(12):
        cases.append(
            owned_case(
                f"MQ{len(cases) + 1:03}",
                "memory_v4_explicit_control",
                "仅查询{A}的支付状态和优惠分摊金额，不办理业务变更。",
                [
                    ev("A", {"status": "PAID"}, "get_order_payment"),
                    ev("A", {"discount_minor": 1000}, "get_order_discounts"),
                ],
                (20, 60, 110, 190)[index % 4],
                11200 + index,
            )
        )
    for length in (12, 45, 90, 175):
        cases.append(
            owned_case(
                f"MQ{len(cases) + 1:03}",
                "known_v2_carrier_omission",
                "查询刚才那个订单的预计送达时间和承运公司",
                [
                    ev("D", {"estimate": "2026-10-10"}, "get_delivery_estimate"),
                    ev("D", {"carrier": "演示物流"}, "get_carrier_contact"),
                ],
                length,
                10403 + length,
                known=True,
            )
        )
    assert len(cases) == 64 and sum(c["known_regression"] for c in cases) == 4
    output.write_text(
        json.dumps(
            {
                "version": "step10-memory-final-v4",
                "review_status": "Prospective new compositions, exposed terminology disclosed; four known regressions scored separately; no independent human blind review",
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
                "new_primary_cases": 60,
                "known_regression_cases": 4,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
