"""Two replicas on one host: API failover, task idempotency and leased worker takeover."""

import asyncio
import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def docker(*args, input=None):
    return subprocess.run(
        ["docker", *args],
        input=input,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    ).stdout


def db(code):
    return docker("exec", "-i", "l4-agent-api-2", "python", "-", input=code)


def lease(run_id):
    return json.loads(
        db(
            """import asyncio,json\nfrom sqlalchemy import text\nfrom app.core.resources import Resources\nfrom app.core.config import get_settings\nasync def main():\n r=Resources(get_settings())\n try:\n  async with r.engine.connect() as c:\n   row=(await c.execute(text("SELECT status,lease_owner,fencing,event_seq FROM agent_runs WHERE id=:id"),{"id":"""
            + repr(run_id)
            + """})).mappings().one()\n   print(json.dumps(dict(row)))\n finally:await r.close()\nasyncio.run(main())"""
        )
    )


async def main():
    output = ROOT / "artifacts/step10/process-redundancy-v2.json"
    report = {
        "captured_at": __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
        "scope": "two API and worker processes on one Docker host; shared singleton DB/Redis/MCP/web; not host or zone HA",
        "checks": {},
    }
    stopped = []
    async with httpx.AsyncClient(
        base_url="http://localhost:8080/api/v1",
        trust_env=False,
        timeout=15,
        limits=httpx.Limits(max_keepalive_connections=0, max_connections=10),
    ) as client:
        login = await client.post(
            "/auth/login",
            json={
                "username": "alice",
                "password": (ROOT / ".secrets/demo_alice_password")
                .read_text(encoding="utf-8")
                .strip(),
            },
        )
        login.raise_for_status()
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        try:
            # Login/session must remain valid when a request is served by another API.
            for _ in range(5):
                assert (await client.get("/auth/session")).status_code == 200
            statuses = []
            samples = []
            finish = asyncio.Event()

            async def traffic():
                while not finish.is_set():
                    started = time.perf_counter()
                    try:
                        statuses.append((await client.get("/runs?limit=5")).status_code)
                    except httpx.HTTPError as exc:
                        statuses.append(0)
                        report.setdefault("transport_errors", []).append(type(exc).__name__)
                    samples.append(
                        {"status": statuses[-1], "elapsed_seconds": time.perf_counter() - started}
                    )
                    await asyncio.sleep(0.05)

            traffic_task = asyncio.create_task(traffic())
            await asyncio.sleep(1)
            await asyncio.to_thread(docker, "stop", "-t", "1", "l4-agent-api-1")
            stopped.append("l4-agent-api-1")
            await asyncio.sleep(6)
            finish.set()
            await traffic_task
            report["api_failure_traffic"] = {
                "requests": len(statuses),
                "codes": {str(code): statuses.count(code) for code in set(statuses)},
                "samples": samples,
            }
            assert statuses and set(statuses) == {200}, report["api_failure_traffic"]
            report["checks"]["api_process_failure_read_failover_and_shared_session"] = True
            order = "HA-" + uuid.uuid4().hex[:12]
            db(
                """import asyncio,json\nfrom sqlalchemy import text\nfrom app.core.config import get_settings\nfrom app.core.resources import Resources\nfrom app.business.fixtures import details\nasync def main():\n r=Resources(get_settings())\n try:\n  async with r.engine.begin() as c:\n   p={"status":"DELIVERED","paid_minor":12900,"refunded_minor":0,"currency":"CNY","delivered_days":2,"return_state":"ACCEPTED","logistics":"已签收","simulator_delay_seconds":2.5,"details":details("""
                + repr(order)
                + """,12900)}\n   await c.execute(text("INSERT INTO commerce_orders VALUES(:id,'customer-alice',1,CAST(:p AS jsonb))"),{"id":"""
                + repr(order)
                + ""","p":json.dumps(p,ensure_ascii=False)})\n finally:await r.close()\nasyncio.run(main())"""
            )
            key = uuid.uuid4().hex
            payload = {"message": "查询" + order + "的订单状态和当前物流，仅查询。"}
            submissions = await asyncio.gather(
                *(
                    client.post("/runs", json=payload, headers={"Idempotency-Key": key})
                    for _ in range(5)
                )
            )
            for response in submissions:
                response.raise_for_status()
            run_ids = {response.json()["id"] for response in submissions}
            assert len(run_ids) == 1
            run_id = run_ids.pop()
            report["workflow_id"] = run_id
            report["checks"]["five_duplicate_submissions_one_durable_task"] = True
            before = None
            for _ in range(80):
                before = await asyncio.to_thread(lease, run_id)
                if before["status"] == "RUNNING" and before["event_seq"] >= 8:
                    break
                await asyncio.sleep(0.2)
            assert before["status"] == "RUNNING"
            workers = {
                name: docker("exec", name, "hostname").strip()
                for name in ("l4-agent-worker-1", "l4-agent-worker-2")
            }
            victim = next(
                name
                for name, hostname in workers.items()
                if before["lease_owner"].startswith(hostname + ":")
            )
            await asyncio.to_thread(docker, "stop", "-t", "1", victim)
            stopped.append(victim)
            takeover_started = time.perf_counter()
            final = None
            for _ in range(150):
                response = await client.get("/runs/" + run_id)
                response.raise_for_status()
                final = response.json()
                if final["status"] in {"SUCCEEDED", "FAILED", "HANDOFF", "PARTIAL", "CANCELLED"}:
                    break
                await asyncio.sleep(0.5)
            after = await asyncio.to_thread(lease, run_id)
            assert final["status"] == "SUCCEEDED", final["status"]
            assert after["fencing"] > before["fencing"]
            assert {e["kind"] for e in final["result"]["entries"]} >= {"get_order", "get_logistics"}
            stream = await client.get("/runs/" + run_id + "/events")
            stream.raise_for_status()
            ids = [int(line[4:]) for line in stream.text.splitlines() if line.startswith("id: ")]
            assert ids == list(range(1, final["event_seq"] + 1))
            report["worker_takeover"] = {
                "stopped_replica": victim,
                "before_fencing": before["fencing"],
                "after_fencing": after["fencing"],
                "stop_to_completion_seconds": time.perf_counter() - takeover_started,
                "terminal_status": final["status"],
                "event_count": len(ids),
                "contiguous_unique_event_seq": True,
            }
            report["checks"]["worker_failure_lease_fencing_checkpoint_takeover"] = True
            report["checks"]["durable_sse_replay_after_takeover"] = True
        finally:
            for name in reversed(stopped):
                await asyncio.to_thread(docker, "start", name)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    asyncio.run(main())
