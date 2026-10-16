"""Freeze memory/recovery component fixtures before any specialist inference."""

import json
from pathlib import Path

from generate_confirmation import case, ev, order

ROOT = Path(__file__).resolve().parents[1]


def write(name, cases):
    path = ROOT / "docs/evaluation" / name
    if path.exists():
        raise RuntimeError("use_new_version_do_not_overwrite")
    path.write_text(
        json.dumps(
            {
                "version": name[:-5],
                "review_status": "project-authored synthetic histories/faults, contracts checked; independent human review pending",
                "cases": cases,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main():
    memory = []
    requests = [
        (
            "最开始那笔订单的订单状态和当前物流",
            "A",
            [
                ev("A", {"status": "DELIVERED"}, "get_order"),
                ev("A", {"status": "已签收"}, "get_logistics"),
            ],
        ),
        (
            "最早那个订单可退多少钱，仅试算不实际退款",
            "A",
            [ev("A", {"amount_minor": 7200}, "quote_refund")],
        ),
        ("第一个订单的发票类型", "A", [ev("A", {"type": "电子普通发票"}, "get_order_invoice")]),
        (
            "最开始那个订单的库存数量",
            "A",
            [ev("A", {"available_quantity": 15}, "get_product_inventory")],
        ),
        ("第二个订单的当前物流", "B", [ev("B", {"status": "在途"}, "get_logistics")]),
        ("第二个订单的保修年限", "B", [ev("B", {"days": 365}, "get_product_warranty")]),
        ("第三个订单的订单状态", "C", [ev("C", {"status": "PAID"}, "get_order")]),
        (
            "第三个订单的脱敏收货地址",
            "C",
            [ev("C", {"city": "杭州市"}, "get_order_address_masked")],
        ),
        ("刚才那个订单的当前物流", "D", [ev("D", {"status": "已签收"}, "get_logistics")]),
        (
            "同一笔订单的剩余可退余额，只查询",
            "D",
            [ev("D", {"remaining_minor": 8400}, "get_refund_balance")],
        ),
    ]
    for length in (20, 50, 100, 150):
        for index, (question, _, evidence) in enumerate(requests):
            identifier = f"MC{len(memory) + 1:03}"
            c = case(
                identifier,
                "memory_reference_" + str(index),
                "请查询" + question,
                {
                    "A": order(7200),
                    "B": order(9100, "SHIPPED"),
                    "C": order(6400, "PAID"),
                    "D": order(8400),
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
            memory.append(c)
    for i in range(10):
        c = case(
            f"MC{len(memory) + 1:03}",
            "memory_explicit_control",
            "请查询{A}的当前物流，仅查询，不退款。",
            {
                "A": order(7200),
                "B": order(9100, "SHIPPED"),
                "C": order(6400, "PAID"),
                "D": order(8400),
            },
            [ev("A", {"status": "已签收"}, "get_logistics")],
            difficulty="long",
        )
        c["history_fixture"] = {
            "turns": (20, 50, 100, 150)[i % 4],
            "kind": "synthetic_frozen_authorized_historical_snapshots",
        }
        c["scripted_user"]["missing_order"] = None
        memory.append(c)
    recovery = []
    for kind in (
        "amount_changed",
        "logistics_changed",
        "eligibility_changed",
        "transient_once",
        "permanent_unavailable",
        "lost_write_response",
    ):
        for i in range(8):
            effects = []
            intents = []
            if kind == "amount_changed":
                message = "查询{A}当前物流并试算可退金额，不提交退款。"
                evidence = [
                    ev("A", {"amount_minor": 15800}, "quote_refund"),
                    ev("A", {"status": "已签收"}, "get_logistics"),
                ]
            elif kind == "logistics_changed":
                message = "查询{A}的订单状态和当前物流。"
                evidence = [ev("A", {"status": "运输异常已更新"}, "get_logistics")]
            elif kind == "eligibility_changed":
                message = "查询{A}当前物流以及售后资格，只查询。"
                evidence = [
                    ev("A", {"eligible": False}, "check_eligibility"),
                    ev("A", {"status": "已签收"}, "get_logistics"),
                ]
            elif kind == "lost_write_response":
                message = "为{A}办理整单退款。"
                evidence = []
                effects = [
                    {"order": "A", "action": "submit_refund", "amount_minor": 7200 + i * 100}
                ]
                intents = [{"order": "A", "action": "submit_refund"}]
            else:
                message = "查询{A}当前订单状态和当前物流。"
                evidence = (
                    []
                    if kind == "permanent_unavailable"
                    else [ev("A", {"status": "已签收"}, "get_logistics")]
                )
            c = case(
                f"RC{len(recovery) + 1:03}",
                "recovery_" + kind,
                message,
                {"A": order(7200 + i * 100)},
                evidence,
                effects,
                statuses=("HANDOFF", "PARTIAL", "FAILED")
                if kind == "permanent_unavailable"
                else ("SUCCEEDED",),
                intents=intents,
                difficulty="fault",
            )
            c["fault"] = kind
            c["recoverable"] = kind != "permanent_unavailable"
            recovery.append(c)
    for i in range(2):
        c = case(
            f"RC{len(recovery) + 1:03}",
            "recovery_no_fault_control",
            "查询{A}的当前物流。",
            {"A": order(7200)},
            [ev("A", {"status": "已签收"}, "get_logistics")],
            difficulty="fault",
        )
        c["recoverable"] = False
        recovery.append(c)
    for i in range(8):
        c = case(
            f"RC{len(recovery) + 1:03}",
            "recovery_expired_observation",
            "查询{A}的订单状态和当前物流。",
            {"A": order(7200 + i * 100)},
            [ev("A", {"status": "已签收"}, "get_logistics")],
            difficulty="fault",
        )
        c["fault"] = "expired_observation"
        c["recoverable"] = True
        recovery.append(c)
    for c in memory:
        c["customer_id"] = "customer-bob"
        for fixture in c["fixture"]["orders"].values():
            fixture["owner"] = "customer-bob"
    write("step10-memory-v1.json", memory)
    write("step10-recovery-v1.json", recovery)
    print(json.dumps({"memory": len(memory), "recovery": len(recovery)}))


if __name__ == "__main__":
    main()
