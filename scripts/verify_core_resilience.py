"""Additional real Docker persistence and dependency fault checks."""

import asyncio
import json
from datetime import UTC, datetime

from verify_agent_core import ROOT, compose, create, login, seed_faults, wait


async def main():
    report = {"captured_at": datetime.now(UTC).isoformat(), "checks": {}}
    client = await login("alice")
    try:
        compose("stop", "worker")
        try:
            source = (ROOT / "scripts/core_store_probe.py").read_text(encoding="utf-8")
            result = compose("exec", "-T", "api", "python", "-", input=source)
            report["checks"].update(json.loads(result))
        finally:
            compose("start", "worker")
        compose("stop", "redis")
        try:
            run = await wait(client, (await create(client, "查询ORD-1001订单状态"))["id"])
            assert run["status"] == "SUCCEEDED", run
            assert (await client.get("/health/ready")).status_code == 503
            report["checks"]["redis_outage_pg_dispatch_continues"] = "passed"
        finally:
            compose("start", "redis")
        seed_faults()
        slow = await create(client, "查询ORD-TEST-SLOW的订单状态、物流和预计退款金额")
        for _ in range(200):
            response = await client.get(f"/runs/{slow['id']}")
            current = response.json()
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
                f"SELECT count(*) FROM agent_events WHERE workflow_id='{slow['id']}' AND payload->>'type'='TOOL_CALL_START'",
            )
            if current["status"] == "RUNNING" and int(count) > 0:
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError("running_boundary_missing")
        response = await client.post(
            f"/runs/{slow['id']}/cancel", json={"expected_revision": current["revision"]}
        )
        assert response.status_code == 202
        assert (await wait(client, slow["id"]))["status"] == "CANCELLED"
        report["checks"]["running_cancel_stops_before_next_tool"] = "passed"
        output = ROOT / "artifacts/step4/resilience-verification.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
