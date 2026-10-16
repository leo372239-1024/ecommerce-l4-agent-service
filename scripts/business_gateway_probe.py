"""Run INSIDE API container. Isolated fixtures, real MCP and authoritative DB invariants."""

import asyncio
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from app.agent.tools import ToolError
from app.business.catalog import CATALOG
from app.business.fixtures import details
from app.business.mcp_client import MerchantClient
from app.business.routing import Router
from app.business.service import MerchantService, one
from app.core.config import get_settings
from app.core.resources import Resources


async def main():
    settings, suffix = get_settings(), uuid.uuid4().hex[:10]
    resources = Resources(settings)
    run_id, order_id = str(uuid.uuid4()), "ORD-S5-" + suffix
    probe_runs = [run_id]
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "fixture_namespace": suffix,
        "checks": {},
        "routes": [],
    }
    p = {
        "status": "DELIVERED",
        "paid_minor": 15900,
        "currency": "CNY",
        "delivered_days": 2,
        "logistics": "已签收",
        "return_state": "ACCEPTED",
        "details": details(order_id, 15900),
    }
    try:
        async with resources.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO commerce_orders VALUES(:id,'customer-alice',1,CAST(:p AS jsonb))"
                ),
                {"id": order_id, "p": json.dumps(p)},
            )
            await conn.execute(
                text(
                    "INSERT INTO agent_runs(id,customer_id,message,request_key,request_hash,status,invocation_id,fencing,lease_until) VALUES(:id,'customer-alice','isolated gateway contract probe',:key,'probe','RUNNING',:invocation,1,now()+interval '1 hour')"
                ),
                {"id": run_id, "key": "s5-" + suffix, "invocation": str(uuid.uuid4())},
            )
        run = {"id": run_id, "fencing": 1, "customer_id": "customer-alice"}
        client, service = MerchantClient(settings, run), MerchantService(resources.engine)
        for key, spec in CATALOG.items():
            if spec.risk == "read":
                value = await client.invoke(key, {"order_id": order_id})
                assert value["schema_version"] == 1 and value["fields"] is not None
        report["checks"]["37_distinct_mcp_reads"] = True
        for foreign in ("ORD-2001", "ORD-NONEXISTENT"):
            try:
                await client.invoke("get_order", {"order_id": foreign})
                raise AssertionError("ownership_escape")
            except ToolError as exc:
                assert exc.code == "order_unavailable"
        report["checks"]["cross_customer_and_missing_indistinguishable"] = True
        router = Router(settings, resources.engine, run)
        route_queries = [
            ("商品保温杯的材质和容量是多少", "get_product_specs"),
            ("发票是什么类型，开具了吗", "get_order_invoice"),
            ("这笔订单用什么方式支付的，支付交易号是什么", "get_order_payment"),
            ("退款还有多少余额，已经退了多少", "get_refund_balance"),
            ("退货后优惠券是否退回", "get_coupon_return_policy"),
            ("退款多久能到账", "get_refund_eta"),
            ("物流轨迹节点和运单号是什么", "get_shipment_tracking"),
            ("退货运费谁付，大概多少", "get_return_shipping_fee"),
            ("所购SKU目前有多少库存", "get_product_inventory"),
            ("订单关联的人工客服工单处理到了哪一步", "get_ticket_status"),
            ("商品保修年限和保修范围是什么", "get_product_warranty"),
            ("查订单原价和优惠分摊", "get_order_discounts"),
        ]
        for query, expected in route_queries:
            selected, args, info = await router.select(query, order_id)
            report["routes"].append(
                {
                    "query": query,
                    "expected": expected,
                    "selected": selected,
                    "correct": selected == expected,
                    "argument_correct": args == {"order_id": order_id},
                    **info,
                }
            )
        proposal = await service.prepare("submit_refund", order_id, run_id, 1, 1, 1)
        args = {
            "order_id": order_id,
            "operation_id": str(proposal["operation_id"]),
            "arg_hash": proposal["arg_hash"],
        }
        try:
            await client.invoke("submit_refund", args)
            raise AssertionError("unapproved_write")
        except ToolError as exc:
            assert exc.code == "approval_invalid_or_stale"
        report["checks"]["unapproved_write_rejected_at_mcp_service"] = True
        async with resources.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE business_proposals SET status='APPROVED',confirmed_by='customer-alice',approved_by='staff-supervisor' WHERE operation_id=:id"
                ),
                {"id": args["operation_id"]},
            )
        # Simultaneous identical operations must serialize, return identical receipt and charge once.
        receipts = await asyncio.gather(*(client.invoke("submit_refund", args) for _ in range(5)))
        assert all(receipt == receipts[0] for receipt in receipts)
        async with resources.engine.connect() as conn:
            row = await one(conn, "SELECT * FROM commerce_orders WHERE id=:id", {"id": order_id})
            count = await conn.scalar(
                text("SELECT count(*) FROM business_operations WHERE operation_id=:id"),
                {"id": args["operation_id"]},
            )
            outbox = await conn.scalar(
                text("SELECT count(*) FROM merchant_outbox WHERE order_id=:id"), {"id": order_id}
            )
        assert (
            row["version"] == 2
            and row["payload"]["refunded_minor"] == 15900
            and count == outbox == 1
        )
        report["checks"]["five_concurrent_dispatches_one_refund_receipt_outbox"] = True
        bad = {**args, "arg_hash": "0" * 64}
        try:
            await client.invoke("submit_refund", bad)
            raise AssertionError("argument_reuse_escape")
        except ToolError as exc:
            assert exc.code == "operation_contract_mismatch"
        report["checks"]["same_operation_changed_arguments_rejected"] = True
        # Synthetic rejected history stresses pagination only, not agent/business success counts.
        async with resources.engine.begin() as conn:
            for version in range(3, 104):
                historical_id = str(uuid.uuid4())
                await conn.execute(
                    text(
                        "INSERT INTO business_proposals(operation_id,workflow_id,customer_id,tenant_id,order_id,action,entity_version,policy_version,amount_minor,arg_hash,status,requires_supervisor) VALUES(:id,:run,'customer-alice','demo-shop',:order,'submit_refund',:version,1,0,:hash,'REJECTED',false)"
                    ),
                    {
                        "id": historical_id,
                        "run": run_id,
                        "order": order_id,
                        "version": version,
                        "hash": "0" * 64,
                    },
                )
                await conn.execute(
                    text(
                        "INSERT INTO business_operations(operation_id,arg_hash,status,receipt) VALUES(:id,:hash,'REJECTED','{}'::jsonb)"
                    ),
                    {"id": historical_id, "hash": "0" * 64},
                )
        latest = await client.invoke("get_operation_receipt", {"order_id": order_id})
        assert len(latest["fields"]["operations"]) == 100 and latest["fields"]["has_more"]
        exact = await client.invoke(
            "get_operation_receipt", {"order_id": order_id, "operation_id": args["operation_id"]}
        )
        assert len(exact["fields"]["operations"]) == 1 and not exact["fields"]["has_more"]
        assert exact["fields"]["operations"][0]["receipt"] == receipts[0]
        missing = await client.invoke(
            "get_operation_receipt", {"order_id": order_id, "operation_id": str(uuid.uuid4())}
        )
        assert not missing["fields"]["operations"]
        report["checks"]["bounded_history_preserves_exact_old_receipt_reconciliation"] = True
        await service.consume_events()
        async with resources.engine.begin() as conn:
            event = await one(
                conn, "SELECT * FROM merchant_outbox WHERE order_id=:id", {"id": order_id}
            )
            await conn.execute(
                text("UPDATE merchant_outbox SET delivered_at=NULL WHERE event_id=:id"),
                {"id": event["event_id"]},
            )
            await conn.execute(
                text(
                    "INSERT INTO environment_versions VALUES(:id,99) ON CONFLICT(order_id) DO UPDATE SET entity_version=99"
                ),
                {"id": order_id},
            )
        await service.consume_events()
        async with resources.engine.connect() as conn:
            version = await conn.scalar(
                text("SELECT entity_version FROM environment_versions WHERE order_id=:id"),
                {"id": order_id},
            )
            count = await conn.scalar(
                text("SELECT count(*) FROM merchant_inbox WHERE event_id=:id"),
                {"id": event["event_id"]},
            )
        assert version == 99 and count == 1
        report["checks"]["duplicate_event_and_version_regression_blocked"] = True
        dual_order = order_id + "-DUAL"
        dual_clients, dual_args = [], []
        async with resources.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO commerce_orders VALUES(:id,'customer-alice',1,CAST(:p AS jsonb))"
                ),
                {"id": dual_order, "p": json.dumps({**p, "paid_minor": 4900})},
            )
        for _ in range(2):
            dual_run = str(uuid.uuid4())
            probe_runs.append(dual_run)
            async with resources.engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO agent_runs(id,customer_id,message,request_key,request_hash,status,invocation_id,fencing,lease_until) VALUES(:id,'customer-alice','isolated competing intent probe',:key,'probe','RUNNING',:invocation,1,now()+interval '1 hour')"
                    ),
                    {"id": dual_run, "key": dual_run, "invocation": str(uuid.uuid4())},
                )
            proposal = await service.prepare("submit_refund", dual_order, dual_run, 1, 1, 1)
            async with resources.engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE business_proposals SET status='APPROVED',confirmed_by='customer-alice' WHERE operation_id=:id"
                    ),
                    {"id": proposal["operation_id"]},
                )
            dual_clients.append(MerchantClient(settings, {"id": dual_run, "fencing": 1}))
            dual_args.append(
                {
                    "order_id": dual_order,
                    "operation_id": str(proposal["operation_id"]),
                    "arg_hash": proposal["arg_hash"],
                }
            )
        competing = await asyncio.gather(
            *(c.invoke("submit_refund", a) for c, a in zip(dual_clients, dual_args, strict=True)),
            return_exceptions=True,
        )
        assert sum(isinstance(item, dict) for item in competing) == 1
        assert (
            sum(
                isinstance(item, ToolError) and item.code == "approval_invalid_or_stale"
                for item in competing
            )
            == 1
        )
        async with resources.engine.connect() as conn:
            paid = await conn.scalar(
                text("SELECT payload->>'refunded_minor' FROM commerce_orders WHERE id=:id"),
                {"id": dual_order},
            )
        assert int(paid) == 4900
        report["checks"]["competing_intents_never_over_refund"] = True
        print(json.dumps(report, ensure_ascii=False))
    finally:
        async with resources.engine.begin() as conn:
            for probe_run in probe_runs:
                await conn.execute(
                    text(
                        "UPDATE agent_runs SET status='HANDOFF',lease_until=NULL,lease_owner=NULL WHERE id=:id"
                    ),
                    {"id": probe_run},
                )
        await resources.close()


if __name__ == "__main__":
    asyncio.run(main())
