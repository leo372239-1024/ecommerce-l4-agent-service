"""Real GLM, REST HITL and Docker process faults. Development acceptance, not benchmark."""

import argparse
import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from verify_agent_core import ROOT, compose, create, login, wait


def db(code):
    return compose("exec", "-T", "api", "python", "-", input=code)


def seed(suffix):
    ids = {
        name: "ORD-S5" + name.upper() + "-" + suffix
        for name in ("return", "cancel", "reject", "stale", "loss", "crash", "cancelafter")
    }
    code = f"""
import asyncio,json
from sqlalchemy import text
from app.core.config import get_settings
from app.core.resources import Resources
from app.business.fixtures import details
async def main():
 r=Resources(get_settings())
 try:
  async with r.engine.begin() as c:
   for name,oid in {ids!r}.items():
    p={{'status':'PAID' if name=='cancel' else 'DELIVERED','paid_minor':12900 if name=='return' else 4900,'currency':'CNY','delivered_days':2,'logistics':'未发货' if name=='cancel' else '已签收','return_state':'NONE' if name in ('return','cancel') else 'ACCEPTED'}}
    p['details']=details(oid,p['paid_minor'])
    if name in ('loss','crash','cancelafter'): p['simulator_write_response_delay_seconds']=20
    await c.execute(text("INSERT INTO commerce_orders VALUES(:id,'customer-alice',1,CAST(:p AS jsonb))"),{{'id':oid,'p':json.dumps(p,ensure_ascii=False)}})
 finally: await r.close()
asyncio.run(main())
"""
    db(code)
    return ids


def observe(order_id):
    code = f"""
import asyncio,json
from sqlalchemy import text
from app.core.config import get_settings
from app.core.resources import Resources
async def main():
 r=Resources(get_settings())
 try:
  async with r.engine.connect() as c:
   row=(await c.execute(text('SELECT version,payload FROM commerce_orders WHERE id=:id'),{{'id':{order_id!r}}})).mappings().one()
   count=await c.scalar(text("SELECT count(*) FROM business_operations b JOIN business_proposals p USING(operation_id) WHERE p.order_id=:id AND b.status='SUCCEEDED'"),{{'id':{order_id!r}}})
   print(json.dumps({{'version':row['version'],'status':row['payload']['status'],'return_state':row['payload'].get('return_state'),'refunded_minor':row['payload'].get('refunded_minor',0),'operation_count':count}}))
 finally: await r.close()
asyncio.run(main())
"""
    return json.loads(db(code).splitlines()[-1])


async def proposal_for(client, order_id, action):
    message = {
        "create_return": "请为订单 " + order_id + " 提交退货申请",
        "submit_refund": "请为已经验收退货的订单 " + order_id + " 执行退款",
        "cancel_order": "请取消未发货的订单 " + order_id,
    }
    run = await create(client, message[action])
    run = await wait(client, run["id"], 180)
    assert run["status"] == "WAITING_APPROVAL", run
    assert run["result"]["proposal"]["action"] == action
    return run, run["result"]["proposal"]


async def decide(client, proposal, decision):
    response = await client.post(
        f"/approvals/{proposal['operation_id']}/decision",
        json={"expected_revision": proposal["revision"], "decision": decision},
    )
    return response


async def main(args):
    suffix = uuid.uuid4().hex[:8]
    ids = seed(suffix)
    customer, supervisor, other, support = (
        await login("alice"),
        await login("supervisor"),
        await login("bob"),
        await login("support"),
    )
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "fixture_namespace": suffix,
        "checks": {},
        "runs": [],
    }
    try:
        run, proposal = await proposal_for(customer, ids["return"], "create_return")
        assert observe(ids["return"])["operation_count"] == 0
        foreign = await other.get(f"/approvals/{proposal['operation_id']}")
        assert foreign.status_code == 404
        denied = await decide(customer, proposal, "approve")
        assert denied.status_code == 403
        response = await decide(customer, proposal, "confirm")
        response.raise_for_status()
        final = await wait(customer, run["id"], 180)
        assert final["status"] == "SUCCEEDED", final
        assert observe(ids["return"])["return_state"] == "REQUESTED"
        report["checks"]["customer_confirm_return_no_early_write_no_role_escalation"] = True
        report["runs"].append(final)
        # Warehouse acceptance is an external merchant fact, never inferred from customer prose.
        db(f"""
import asyncio,json,uuid
from sqlalchemy import text
from app.core.resources import Resources
from app.core.config import get_settings
async def main():
 r=Resources(get_settings())
 try:
  async with r.engine.begin() as c:
   version=await c.scalar(text("UPDATE commerce_orders SET version=version+1,payload=payload || CAST(:p AS jsonb) WHERE id=:id RETURNING version"),{{'id':{ids["return"]!r},'p':json.dumps({{'return_state':'ACCEPTED'}})}})
   await c.execute(text('INSERT INTO merchant_outbox(event_id,order_id,entity_version,payload) VALUES(:event,:order,:version,CAST(:payload AS jsonb))'),{{'event':str(uuid.uuid4()),'order':{ids["return"]!r},'version':version,'payload':json.dumps({{'type':'warehouse.accepted'}})}})
 finally: await r.close()
asyncio.run(main())
""")
        run, proposal = await proposal_for(customer, ids["return"], "submit_refund")
        assert proposal["requires_supervisor"]
        response = await decide(supervisor, proposal, "approve")
        assert response.status_code == 403
        confirmed = await decide(customer, proposal, "confirm")
        confirmed.raise_for_status()
        assert confirmed.json()["status"] == "CONFIRMED"
        assert observe(ids["return"])["refunded_minor"] == 0
        approved = await decide(supervisor, confirmed.json(), "approve")
        approved.raise_for_status()
        final = await wait(customer, run["id"], 180)
        assert final["status"] == "SUCCEEDED", final
        assert observe(ids["return"])["refunded_minor"] == 12900
        report["checks"][
            "return_inspection_customer_confirmation_supervisor_refund_closed_loop"
        ] = True
        report["runs"].append(final)
        run, proposal = await proposal_for(customer, ids["cancel"], "cancel_order")
        response = await decide(customer, proposal, "confirm")
        response.raise_for_status()
        final = await wait(customer, run["id"], 180)
        result = observe(ids["cancel"])
        assert (
            final["status"] == "SUCCEEDED"
            and result["status"] == "CANCELLED"
            and result["refunded_minor"] == 4900
        )
        report["checks"]["cancel_unshipped_order_and_refund_atomic"] = True
        report["runs"].append(final)
        run, proposal = await proposal_for(customer, ids["reject"], "submit_refund")
        (await decide(customer, proposal, "reject")).raise_for_status()
        final = await wait(customer, run["id"], 180)
        assert observe(ids["reject"])["operation_count"] == 0
        report["runs"].append(final)
        report["checks"]["customer_rejection_never_dispatches"] = True
        run, proposal = await proposal_for(customer, ids["stale"], "submit_refund")
        db(f"""
import asyncio
from sqlalchemy import text
from app.core.resources import Resources
from app.core.config import get_settings
async def main():
 r=Resources(get_settings())
 try:
  async with r.engine.begin() as c: await c.execute(text('UPDATE commerce_orders SET version=version+1 WHERE id=:id'),{{'id':{ids["stale"]!r}}})
 finally: await r.close()
asyncio.run(main())
""")
        stale = await decide(customer, proposal, "confirm")
        assert (
            stale.status_code == 409
            and stale.json()["detail"] == "proposal_invalidated_reconfirm_new_intent"
        )
        stale_final = await wait(customer, run["id"], 180)
        report["runs"].append(stale_final)
        assert observe(ids["stale"])["operation_count"] == 0
        report["checks"]["stale_quote_invalidates_confirmation"] = True
        case_id = stale_final["result"]["handoff_case_id"]
        assert (await other.get(f"/cases/{case_id}")).status_code == 404
        case = (await customer.get(f"/cases/{case_id}")).json()
        assert (
            await customer.post(
                f"/cases/{case_id}/claim", json={"expected_revision": case["revision"]}
            )
        ).status_code == 403
        claimed = await support.post(
            f"/cases/{case_id}/claim", json={"expected_revision": case["revision"]}
        )
        claimed.raise_for_status()
        case = claimed.json()
        # Fixture models dispatch interrupted before the remote transaction begins.
        db(f"""
import asyncio
from sqlalchemy import text
from app.core.resources import Resources
from app.core.config import get_settings
async def main():
 r=Resources(get_settings())
 try:
  async with r.engine.begin() as c:
   await c.execute(text("INSERT INTO business_operations(operation_id,arg_hash,status) SELECT operation_id,arg_hash,'RECONCILING' FROM business_proposals WHERE operation_id=:id"),{{'id':{proposal["operation_id"]!r}}})
 finally: await r.close()
asyncio.run(main())
""")
        blocked = await support.post(
            f"/cases/{case_id}/close",
            json={
                "expected_revision": case["revision"],
                "resolution": "确认未退款，旧报价失效，需重新确认。",
            },
        )
        assert blocked.status_code == 409
        reconciled = await support.post(
            f"/cases/{case_id}/reconcile", json={"expected_revision": case["revision"]}
        )
        reconciled.raise_for_status()
        closed = await support.post(
            f"/cases/{case_id}/close",
            json={
                "expected_revision": reconciled.json()["revision"],
                "resolution": "已按商家事务记录核对未发生退款，旧报价失效，需重新确认。",
            },
        )
        closed.raise_for_status()
        assert closed.json()["status"] == "CLOSED"
        assert (await customer.get(f"/cases/{case_id}")).json()["status"] == "CLOSED"
        report["checks"][
            "staff_claim_reconcile_close_customer_visible_no_unknown_effect_hidden"
        ] = True
        if args.faults:
            for name in ("loss", "crash", "cancelafter"):
                run, proposal = await proposal_for(customer, ids[name], "submit_refund")
                (await decide(customer, proposal, "confirm")).raise_for_status()
                deadline = time.monotonic() + 30
                while observe(ids[name])["operation_count"] == 0:
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.2)
                if name == "crash":
                    compose("kill", "-s", "SIGKILL", "worker")
                    compose("start", "worker")
                if name == "cancelafter":
                    current = (await customer.get(f"/runs/{run['id']}")).json()
                    response = await customer.post(
                        f"/runs/{run['id']}/cancel", json={"expected_revision": current["revision"]}
                    )
                    response.raise_for_status()
                final = await wait(customer, run["id"], 180)
                result = observe(ids[name])
                assert final["status"] == ("PARTIAL" if name == "cancelafter" else "SUCCEEDED"), (
                    final
                )
                if name == "cancelafter":
                    assert final["result"]["completed_operations"]
                assert (
                    result["refunded_minor"] == 4900
                    and result["operation_count"] == 1
                    and result["version"] == 2
                )
                report["checks"][
                    {
                        "loss": "lost_response_reconciles_without_duplicate_refund",
                        "crash": "worker_kill_after_commit_checkpoint_recovery_once",
                        "cancelafter": "cancel_after_commit_preserves_completed_effect_truth",
                    }[name]
                ] = True
                report["runs"].append(final)
        target = args.output_dir
        target.mkdir(parents=True, exist_ok=True)
        (target / "business-flow-verification.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({"checks": report["checks"], "actual_model_runs": len(report["runs"])}))
    finally:
        for client in (customer, supervisor, other, support):
            await client.aclose()
        if args.faults:
            compose("start", "worker", "merchant-mcp")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--faults", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/step5")
    asyncio.run(main(parser.parse_args()))
