"""An external simulator update must invalidate old evidence and trigger scoped replanning."""

import asyncio
import json
import uuid
from datetime import UTC, datetime

from verify_agent_core import ROOT, compose, create, login, wait


async def main():
    order = "ORD-CHANGE-" + uuid.uuid4().hex[:8]
    source = """
import asyncio,json,sys
from sqlalchemy import text
from app.core.config import get_settings
from app.core.resources import Resources
async def main():
 r=Resources(get_settings())
 try:
  async with r.engine.begin() as c:
   p={'status':'DELIVERED','paid_minor':12900,'currency':'CNY','delivered_days':3,'logistics':'已签收','simulator_delay_seconds':1.5}
   await c.execute(text('INSERT INTO commerce_orders VALUES (:id,:owner,1,CAST(:payload AS jsonb))'),{'id':sys.argv[1],'owner':'customer-alice','payload':json.dumps(p)})
 finally: await r.close()
asyncio.run(main())
"""
    compose("exec", "-T", "api", "python", "-", order, input=source)
    client = await login("alice")
    try:
        run = await create(client, f"查询{order}的订单状态、物流、售后资格和预计退款金额")
        for _ in range(160):
            count = compose(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "agent",
                "-d",
                "agent",
                "-Atc",
                f"SELECT count(*) FROM agent_events WHERE workflow_id='{run['id']}' AND payload->>'type'='TOOL_CALL_RESULT'",
            )
            if int(count):
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError("environment_boundary_missing")
        changed = compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "agent",
            "-d",
            "agent",
            "-Atc",
            f"UPDATE commerce_orders SET version=2,payload=jsonb_set(payload,'{{paid_minor}}','15900') WHERE id='{order}' AND version=1 RETURNING version",
        )
        assert changed.startswith("2")
        final = await wait(client, run["id"])
        assert final["status"] == "SUCCEEDED", final
        quote = next(
            e["evidence"] for e in final["result"]["entries"] if e["kind"] == "quote_refund"
        )
        assert quote["fields"]["amount_minor"] == 15900 and quote["entity_version"] == 2
        assert all(e["evidence"]["entity_version"] == 2 for e in final["result"]["entries"])
        events = await client.get(f"/runs/{run['id']}/events")
        parsed = [
            json.loads(line[6:]) for line in events.text.splitlines() if line.startswith("data: ")
        ]
        reflection = [
            event["value"] for event in parsed if event.get("name") == "business.reflection"
        ]
        assert reflection and reflection[0]["issue"] == "stale_environment"
        assert (
            reflection[0]["action"] == "refresh_order"
            and reflection[0]["proposal_validated"] is True
        )
        report = {
            "captured_at": datetime.now(UTC).isoformat(),
            "workflow_id": run["id"],
            "before_version": 1,
            "after_version": 2,
            "quote_minor": 15900,
            "all_final_order_evidence_version_2": True,
            "reflection": reflection,
            "usage": final["result"]["usage"],
        }
        output = ROOT / "artifacts/step4/environment-change-verification.json"
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
