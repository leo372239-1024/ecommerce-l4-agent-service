"""Container-only safety fault fixtures use their own tenant and accounts."""

import asyncio
import json
import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy import text

from app.agent.tools import ToolError
from app.business.mcp_client import MerchantClient
from app.business.service import MerchantService
from app.core.config import get_settings
from app.core.resources import Resources


async def main():
    settings = get_settings()
    resources = Resources(settings)
    suffix = uuid.uuid4().hex[:8]
    results = {}
    # Moving the account must not move previously created merchant orders.
    scenarios = {
        "expired_approval": "approval_invalid_or_stale",
        "policy_changed": "approval_invalid_or_stale",
        "order_changed": "approval_invalid_or_stale",
        "customer_revoked": "permission_revoked",
        "supervisor_revoked": "supervisor_authority_revoked",
        "obsolete_fence": "stale_execution_authority",
        "cancelled_execution": "run_cancelled",
    }
    scenarios["customer_tenant_changed"] = "order_unavailable"
    try:
        async with httpx.AsyncClient(timeout=3) as http:
            response = await http.post(settings.mcp_url, json={})
            assert response.status_code == 401
        results["mcp_requires_independent_service_authentication"] = True
        for index, (scenario, expected) in enumerate(scenarios.items()):
            tenant = f"s5-security-{suffix}-{index}"
            customer, supervisor = tenant + "-customer", tenant + "-supervisor"
            order, run_id = "ORD-S5SEC-" + suffix + "-" + str(index), str(uuid.uuid4())
            p = {
                "status": "DELIVERED",
                "paid_minor": 15900 if scenario == "supervisor_revoked" else 4900,
                "currency": "CNY",
                "delivered_days": 2,
                "logistics": "已签收",
                "return_state": "ACCEPTED",
            }
            async with resources.engine.begin() as conn:
                for actor, role in ((customer, "customer"), (supervisor, "supervisor")):
                    await conn.execute(
                        text(
                            "INSERT INTO customer_accounts(id,username,password_hash,role,tenant_id) SELECT :id,:id,password_hash,:role,:tenant FROM customer_accounts WHERE id='customer-alice'"
                        ),
                        {"id": actor, "role": role, "tenant": tenant},
                    )
                await conn.execute(
                    text("INSERT INTO merchant_policies VALUES(:tenant,1,7,10000)"),
                    {"tenant": tenant},
                )
                await conn.execute(
                    text("INSERT INTO commerce_orders VALUES(:id,:owner,1,CAST(:p AS jsonb))"),
                    {"id": order, "owner": customer, "p": json.dumps(p)},
                )
                await conn.execute(
                    text(
                        "INSERT INTO agent_runs(id,customer_id,message,request_key,request_hash,status,invocation_id,fencing,lease_until) VALUES(:id,:owner,'isolated safety probe',:key,'probe','RUNNING',:invocation,1,now()+interval '1 hour')"
                    ),
                    {
                        "id": run_id,
                        "owner": customer,
                        "key": tenant,
                        "invocation": str(uuid.uuid4()),
                    },
                )
            proposal = await MerchantService(resources.engine).prepare(
                "submit_refund", order, run_id, 1, 1, 1
            )
            async with resources.engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE business_proposals SET status='APPROVED',confirmed_by=:owner,approved_by=:staff WHERE operation_id=:id"
                    ),
                    {"id": proposal["operation_id"], "owner": customer, "staff": supervisor},
                )
                if scenario == "expired_approval":
                    await conn.execute(
                        text(
                            "UPDATE business_proposals SET expires_at=now()-interval '1 second' WHERE operation_id=:id"
                        ),
                        {"id": proposal["operation_id"]},
                    )
                elif scenario == "policy_changed":
                    await conn.execute(
                        text("UPDATE merchant_policies SET version=2 WHERE tenant_id=:tenant"),
                        {"tenant": tenant},
                    )
                elif scenario == "order_changed":
                    await conn.execute(
                        text("UPDATE commerce_orders SET version=2 WHERE id=:id"), {"id": order}
                    )
                elif scenario in {"customer_revoked", "supervisor_revoked"}:
                    await conn.execute(
                        text("UPDATE customer_accounts SET active=false WHERE id=:id"),
                        {"id": supervisor if scenario == "supervisor_revoked" else customer},
                    )
                elif scenario == "cancelled_execution":
                    await conn.execute(
                        text("UPDATE agent_runs SET cancel_requested=true WHERE id=:id"),
                        {"id": run_id},
                    )
                elif scenario == "customer_tenant_changed":
                    await conn.execute(
                        text("INSERT INTO merchant_policies VALUES(:tenant,1,7,10000)"),
                        {"tenant": tenant + "-other"},
                    )
                    await conn.execute(
                        text("UPDATE customer_accounts SET tenant_id=:tenant WHERE id=:id"),
                        {"id": customer, "tenant": tenant + "-other"},
                    )
            client = MerchantClient(
                settings, {"id": run_id, "fencing": 2 if scenario == "obsolete_fence" else 1}
            )
            try:
                await client.invoke(
                    "submit_refund",
                    {
                        "order_id": order,
                        "operation_id": str(proposal["operation_id"]),
                        "arg_hash": proposal["arg_hash"],
                    },
                )
                raise AssertionError("unsafe_dispatch:" + scenario)
            except ToolError as exc:
                assert exc.code == expected, (scenario, exc.code)
            if scenario == "policy_changed":
                from types import SimpleNamespace

                from app.agent.contracts import Goal, TaskSpec, compile_plan
                from app.business.gateway import Gateway

                gateway = Gateway(
                    settings,
                    SimpleNamespace(engine=resources.engine),
                    {"id": run_id, "fencing": 1, "customer_id": customer},
                )
                node = compile_plan(TaskSpec(order_ids=[order], goals=[Goal.ELIGIBILITY])).nodes[-1]
                evidence = {
                    f"{order}:get_order": {"entity_version": 1},
                    f"{order}:get_policy": {
                        "entity_id": "return-policy:" + tenant,
                        "entity_version": 1,
                    },
                }
                try:
                    await gateway.execute(node, evidence)
                    raise AssertionError("mixed_policy_evidence")
                except ToolError as exc:
                    assert exc.code == "stale_environment"
                results["policy_version_change_invalidates_read_dependency"] = True
            async with resources.engine.begin() as conn:
                value = await conn.scalar(
                    text("SELECT payload FROM commerce_orders WHERE id=:id"), {"id": order}
                )
                count = await conn.scalar(
                    text("SELECT count(*) FROM business_operations WHERE operation_id=:id"),
                    {"id": proposal["operation_id"]},
                )
                assert not value.get("refunded_minor") and count == 0
                await conn.execute(
                    text("UPDATE agent_runs SET status='HANDOFF',lease_until=NULL WHERE id=:id"),
                    {"id": run_id},
                )
            results[scenario] = True
        print(
            json.dumps(
                {
                    "captured_at": datetime.now(UTC).isoformat(),
                    "fixture_namespace": suffix,
                    "checks": results,
                }
            )
        )
    finally:
        await resources.close()


if __name__ == "__main__":
    asyncio.run(main())
