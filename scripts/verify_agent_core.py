"""Real Docker + GLM smoke and safety checks. Not the frozen quality benchmark."""

import argparse
import asyncio
import json
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://localhost:8080/api/v1"
TERMINAL = {
    "SUCCEEDED",
    "PARTIAL",
    "HANDOFF",
    "FAILED",
    "CANCELLED",
    "WAITING_USER",
    "WAITING_APPROVAL",
}


async def login(name):
    client = httpx.AsyncClient(base_url=BASE, timeout=15, trust_env=False)
    response = await client.post(
        "/auth/login",
        json={
            "username": name,
            "password": (ROOT / ".secrets" / f"demo_{name}_password").read_text().strip(),
        },
    )
    response.raise_for_status()
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client


async def create(client, message, key=None):
    response = await client.post(
        "/runs", json={"message": message}, headers={"Idempotency-Key": key or uuid.uuid4().hex}
    )
    response.raise_for_status()
    return response.json()


async def wait(client, run_id, timeout=80):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = await client.get(f"/runs/{run_id}")
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException):
            await asyncio.sleep(0.25)
            continue
        if response.status_code in {502, 503, 504}:
            await asyncio.sleep(0.25)
            continue
        response.raise_for_status()
        run = response.json()
        if run["status"] in TERMINAL:
            return run
        await asyncio.sleep(0.25)
    raise AssertionError("run_completion_timeout")


def compose(*args, input=None):
    command = [
        "docker",
        "compose",
        "--project-name",
        "l4-agent",
        "--env-file",
        str(ROOT / ".env"),
        "--env-file",
        str(ROOT / "infra/images.env"),
        "-f",
        str(ROOT / "infra/docker-compose.yml"),
        *args,
    ]
    result = subprocess.run(
        command, input=input, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return result.stdout.strip()


def seed_faults():
    # Fixed dedicated test orders only; never change merchant fixtures or existing services.
    code = """
import asyncio,json
from sqlalchemy import text
from app.core.config import get_settings
from app.core.resources import Resources
async def main():
 r=Resources(get_settings())
 try:
  async with r.engine.begin() as c:
   for name,extra in [('ORD-TEST-SLOW',{'simulator_delay_seconds':6}),('ORD-TEST-ERROR',{'simulator_transient_error':True})]:
    payload={'status':'DELIVERED','paid_minor':12900,'currency':'CNY','delivered_days':3,'logistics':'已签收',**extra}
    await c.execute(text('INSERT INTO commerce_orders VALUES (:id,:owner,1,CAST(:payload AS jsonb)) ON CONFLICT (id) DO NOTHING'),{'id':name,'owner':'customer-alice','payload':json.dumps(payload)})
 finally: await r.close()
asyncio.run(main())
"""
    # Avoid host shell quoting and never interpolate secrets.
    compose("exec", "-T", "api", "python", "-", input=code)


async def main(faults):
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "model": "glm-4-flash",
        "business": "seeded_commerce_simulator",
        "cases": [],
        "checks": {},
    }
    alice, bob = await login("alice"), await login("bob")
    try:
        async with httpx.AsyncClient(base_url=BASE) as anonymous:
            assert (await anonymous.get("/runs")).status_code == 401
        csrf = alice.headers.pop("X-CSRF-Token")
        assert (
            await alice.post(
                "/runs", json={"message": "查询"}, headers={"Idempotency-Key": uuid.uuid4().hex}
            )
        ).status_code == 403
        alice.headers["X-CSRF-Token"] = csrf
        report["checks"]["authentication_csrf"] = "passed"
        cases = [
            (
                "multi_goal_quote",
                "查询ORD-1001的订单状态、物流、是否符合售后政策和预计退款金额",
                "SUCCEEDED",
            ),
            ("scoped_multi_order", "只查ORD-1001的预计退款金额，只查ORD-1002的物流", "SUCCEEDED"),
            ("policy_denial", "查询ORD-1003的预计退款金额", "SUCCEEDED"),
            ("cross_customer", "查询ORD-2001订单状态和物流", "FAILED"),
            ("write_handoff", "请立即提交ORD-1001的退款申请", "HANDOFF"),
        ]
        for name, message, expected in cases:
            start = time.monotonic()
            run = await wait(alice, (await create(alice, message))["id"])
            assert run["status"] == expected, (name, run)
            if name == "multi_goal_quote":
                quote = next(
                    e["evidence"]["fields"]
                    for e in run["result"]["entries"]
                    if e["kind"] == "quote_refund"
                )
                assert quote["amount_minor"] == 12900 and quote["is_estimate"] is True
            if name == "scoped_multi_order":
                kinds = {(e["order_id"], e["kind"]) for e in run["result"]["entries"]}
                assert ("ORD-1002", "quote_refund") not in kinds and (
                    "ORD-1001",
                    "get_logistics",
                ) not in kinds
            if name == "cross_customer":
                assert not run["result"]["entries"] and "21900" not in json.dumps(run)
            response = await alice.get(f"/runs/{run['id']}/events")
            response.raise_for_status()
            seq = [int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")]
            assert seq == list(range(1, run["event_seq"] + 1))
            parsed = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            assert sum(e["type"] == "RUN_FINISHED" for e in parsed) == 1
            cursor = seq[len(seq) // 2]
            resumed = await alice.get(
                f"/runs/{run['id']}/events", headers={"Last-Event-ID": str(cursor)}
            )
            tail = [int(line[4:]) for line in resumed.text.splitlines() if line.startswith("id: ")]
            assert tail == list(range(cursor + 1, run["event_seq"] + 1))
            assert (await bob.get(f"/runs/{run['id']}")).status_code == 404
            assert (await bob.get(f"/runs/{run['id']}/events")).status_code == 404
            report["cases"].append(
                {
                    "name": name,
                    "status": run["status"],
                    "workflow_id": run["id"],
                    "elapsed_seconds": round(time.monotonic() - start, 3),
                    "usage": run["result"].get("usage", {}),
                    "event_count": len(seq),
                }
            )
        report["checks"]["ag_ui_cursor_replay_and_owner_isolation"] = "passed"
        key = uuid.uuid4().hex
        duplicated = await asyncio.gather(
            *[create(alice, "查询ORD-1001订单状态", key) for _ in range(8)]
        )
        assert len({run["id"] for run in duplicated}) == 1
        assert (
            await alice.post(
                "/runs", json={"message": "不同消息"}, headers={"Idempotency-Key": key}
            )
        ).status_code == 409
        await wait(alice, duplicated[0]["id"])
        report["checks"]["eight_concurrent_create_idempotency"] = "passed"
        waiting = await wait(alice, (await create(alice, "帮我查一下订单物流"))["id"])
        assert waiting["status"] == "WAITING_USER"
        # Restart both API and worker while suspended: no in-process state may be required.
        compose("restart", "api", "worker")
        await asyncio.sleep(2)
        payload = {"expected_revision": waiting["revision"], "input": {"order_ids": ["ORD-1001"]}}
        replies = await asyncio.gather(
            *[alice.post(f"/runs/{waiting['id']}/resume", json=payload) for _ in range(2)]
        )
        assert sorted(r.status_code for r in replies) == [202, 409]
        completed = await wait(alice, waiting["id"])
        assert completed["status"] == "SUCCEEDED" and completed["generation"] == 2
        report["checks"]["persisted_interrupt_restart_cas_resume"] = "passed"
        # Cancellation before the worker claims requires no provider or tool activity.
        compose("stop", "worker")
        try:
            queued = await create(alice, "查询ORD-1001的物流")
            cancelled = await alice.post(
                f"/runs/{queued['id']}/cancel", json={"expected_revision": queued["revision"]}
            )
            assert cancelled.status_code == 202 and cancelled.json()["status"] == "CANCELLED"
        finally:
            compose("start", "worker")
        report["checks"]["queued_cancel"] = "passed"
        if faults:
            seed_faults()
            error_run = await wait(alice, (await create(alice, "查询ORD-TEST-ERROR订单状态"))["id"])
            assert error_run["status"] == "FAILED"
            assert error_run["result"]["usage"]["tool_calls"] == 2
            trajectory = await alice.get(f"/runs/{error_run['id']}/events")
            reflections = [
                json.loads(line[6:])["value"]
                for line in trajectory.text.splitlines()
                if line.startswith("data: ")
                and json.loads(line[6:]).get("name") == "business.reflection"
            ]
            assert reflections[0]["proposal_validated"] is True
            assert reflections[-1]["no_progress_count"] == 2
            report["checks"]["real_model_reflection_no_progress"] = "passed"
            report["reflection_fault"] = {"workflow_id": error_run["id"], "decisions": reflections}
            slow = await create(alice, "查询ORD-TEST-SLOW的订单状态、物流和预计退款金额")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                # DB query is a test observer, not part of agent access permissions.
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
                    f"SELECT count(*) FROM agent_events WHERE workflow_id='{slow['id']}' AND payload->>'type'='TOOL_CALL_RESULT'",
                )
                if int(count) >= 1:
                    break
                await asyncio.sleep(0.25)
            else:
                raise AssertionError("crash_boundary_not_reached")
            start = time.monotonic()
            compose("kill", "-s", "SIGKILL", "worker")
            try:
                compose("start", "worker")
                restored = await wait(alice, slow["id"], 110)
                assert restored["status"] == "SUCCEEDED", restored
                fence = compose(
                    "exec",
                    "-T",
                    "postgres",
                    "psql",
                    "-U",
                    "agent",
                    "-d",
                    "agent",
                    "-Atc",
                    f"SELECT fencing FROM agent_runs WHERE id='{slow['id']}'",
                )
                assert int(fence) >= 2
                report["checks"]["worker_sigkill_checkpoint_recovery"] = {
                    "fencing": int(fence),
                    "recovery_seconds": round(time.monotonic() - start, 3),
                }
            finally:
                compose("start", "worker")
        output = ROOT / "artifacts/step4/agent-core-verification.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        await alice.aclose()
        await bob.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--faults", action="store_true")
    asyncio.run(main(parser.parse_args().faults))
