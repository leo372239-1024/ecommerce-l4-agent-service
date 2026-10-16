"""Verify fault injection boundaries with real MCP/DB, without model inference."""

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text

from app.agent.contracts import Goal, PlanNode
from app.agent.runtime import Harness
from app.agent.store import RunStore
from app.agent.tools import ToolError, verify
from app.api.routes.approvals import Decision, decide
from app.business.fixtures import details
from app.core.auth import Principal
from app.core.config import get_settings
from app.core.resources import Resources
from app.evaluation.specialists import FaultController


async def main():
    settings = get_settings()
    assert settings.postgres_db == "agent_benchmark_step9"
    r = Resources(settings)
    store = RunStore(r.engine)
    checks = []
    try:
        for kind in (
            "amount_changed",
            "logistics_changed",
            "eligibility_changed",
            "transient_once",
            "permanent_unavailable",
            "expired_observation",
            "lost_write_response",
        ):
            order = "CTRL-" + uuid.uuid4().hex[:12]
            payload = {
                "status": "DELIVERED",
                "paid_minor": 7200,
                "refunded_minor": 0,
                "currency": "CNY",
                "delivered_days": 2,
                "return_state": "ACCEPTED",
                "logistics": "已签收",
                "details": details(order, 7200),
            }
            async with r.engine.begin() as c:
                await c.execute(
                    text(
                        "INSERT INTO commerce_orders VALUES(:id,'customer-alice',1,CAST(:p AS jsonb))"
                    ),
                    {"id": order, "p": json.dumps(payload, ensure_ascii=False)},
                )
            row = await store.create(
                "customer-alice",
                "组件故障探针 " + order,
                uuid.uuid4().hex,
                hashlib.sha256(order.encode()).hexdigest(),
                "evaluation",
            )
            row = await store.claim("controller-probe", 120, row["id"])
            ctrl = FaultController(r.engine, kind, {"A": order})
            h = ctrl.wrap(Harness)(settings, store, row)

            def node(tool, order=order):
                return PlanNode(
                    id=order + ":" + tool,
                    tool=tool,
                    goal=Goal.REFUND
                    if tool == "submit_refund"
                    else Goal.LOGISTICS
                    if tool == "get_logistics"
                    else Goal.ORDER,
                    order_id=order,
                    dependencies=[],
                    preconditions=[],
                    postconditions=[],
                    risk="write" if tool == "submit_refund" else "read",
                )

            obs = await h.gateway.client.invoke("get_order", {"order_id": order})
            evidence = {order + ":get_order": obs}
            if kind == "lost_write_response":
                policy = await h.gateway.client.invoke("get_policy", {"order_id": order})
                evidence[order + ":get_policy"] = policy
                evidence[order + ":check_eligibility"] = await h.gateway.client.invoke(
                    "check_eligibility", {"order_id": order}
                )
                proposal = await h.gateway.prepare(node("submit_refund"), evidence)
                request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(resources=r)))
                # Approval route expects a durable waiting task.
                async with r.engine.begin() as c:
                    await c.execute(
                        text("UPDATE agent_runs SET status='WAITING_APPROVAL' WHERE id=:id"),
                        {"id": row["id"]},
                    )
                confirmed = await decide(
                    proposal["operation_id"],
                    Decision(expected_revision=proposal["revision"], decision="confirm"),
                    request,
                    Principal("customer-alice", "customer", "demo-shop"),
                )
                await decide(
                    proposal["operation_id"],
                    Decision(expected_revision=confirmed["revision"], decision="approve"),
                    request,
                    Principal("staff-supervisor", "supervisor", "demo-shop"),
                )
                # Reclaim the legitimate resumed lease and attach controller to that authority.
                row = await store.claim("controller-probe", 120, row["id"])
                h = ctrl.wrap(Harness)(settings, store, row)
                receipt, info = await h.gateway.commit(node("submit_refund"), proposal)
                assert info["reconciled"] and receipt["fields"]["amount_minor"] == 7200
                async with r.engine.connect() as c:
                    assert (
                        await c.scalar(
                            text(
                                "SELECT count(*) FROM business_operations b JOIN business_proposals p USING(operation_id) WHERE p.workflow_id=:id AND b.status='SUCCEEDED'"
                            ),
                            {"id": row["id"]},
                        )
                        == 1
                    )
            else:
                try:
                    result = await h.gateway.execute(node("get_logistics"), evidence)
                    verify(node("get_logistics"), result, evidence)
                    raise AssertionError("fault_not_injected")
                except ToolError as error:
                    expected = (
                        "stale_environment"
                        if kind.endswith("changed")
                        else "expired_evidence"
                        if kind == "expired_observation"
                        else "upstream_unavailable"
                    )
                    assert error.code == expected, (kind, error.code)
                if kind != "permanent_unavailable":
                    fresh = await h.gateway.client.invoke("get_order", {"order_id": order})
                    result = await h.gateway.execute(
                        node("get_logistics"), {order + ":get_order": fresh}
                    )
                    verify(node("get_logistics"), result, {order + ":get_order": fresh})
            await store.finish(
                row["id"],
                row["fencing"],
                "SUCCEEDED",
                {"component_probe": True},
                h.terminal({"component_probe": True}),
            )
            checks.append({"kind": kind, "passed": True, "controller_events": ctrl.events})
    finally:
        await r.close()
    out = Path("/artifacts/step10/controller-boundary-preflight.json")
    out.write_text(
        json.dumps(
            {"model_inference": False, "agent_quality_score": None, "checks": checks},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(json.dumps({"checks": len(checks), "passed": True}))


if __name__ == "__main__":
    asyncio.run(main())
