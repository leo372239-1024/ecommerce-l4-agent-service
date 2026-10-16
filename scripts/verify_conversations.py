"""Real Docker/model conversation continuity and isolation, using read-only business tasks."""

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://localhost:8080/api/v1"


async def login(name):
    c = httpx.AsyncClient(base_url=BASE, timeout=15, trust_env=False)
    r = await c.post(
        "/auth/login",
        json={
            "username": name,
            "password": (ROOT / f".secrets/demo_{name}_password").read_text().strip(),
        },
    )
    r.raise_for_status()
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return c


async def wait(c, run):
    for _ in range(180):
        r = await c.get(f"/runs/{run['id']}")
        r.raise_for_status()
        row = r.json()
        if row["status"] not in {"QUEUED", "RUNNING"}:
            return row
        await asyncio.sleep(1)
    raise RuntimeError("conversation_task_timeout")


async def main():
    report = {"captured_at": datetime.now(UTC).isoformat(), "checks": [], "runs": []}
    out = ROOT / "artifacts/step10/conversation-acceptance.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    def check(name, condition):
        report["checks"].append({"name": name, "passed": bool(condition)})
        print(json.dumps({"name": name, "passed": bool(condition)}), flush=True)
        if not condition:
            raise AssertionError(name)

    alice, bob, support = [await login(n) for n in ("alice", "bob", "support")]
    try:
        r = await alice.post("/conversations", json={})
        r.raise_for_status()
        cid = r.json()["id"]
        check(
            "foreign_conversation_hidden",
            (await bob.get(f"/conversations/{cid}")).status_code == 404,
        )
        check(
            "staff_cannot_create_customer_conversation",
            (await support.post("/conversations", json={})).status_code == 403,
        )
        body = {
            "message": "查询ORD-1001的订单状态",
            "conversation_id": cid,
            "dataset_partition": "evaluation",
        }
        key = uuid.uuid4().hex
        r = await alice.post("/runs", json=body, headers={"Idempotency-Key": key})
        r.raise_for_status()
        first = r.json()
        duplicate = await alice.post("/runs", json=body, headers={"Idempotency-Key": key})
        check(
            "retry_keeps_same_run_and_sequence",
            duplicate.status_code == 202
            and duplicate.json()["id"] == first["id"]
            and duplicate.json()["conversation_seq"] == 1,
        )
        r = await alice.post(
            "/runs",
            json={**body, "message": "查物流"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        check("conversation_runs_serialized", r.status_code == 409)
        r = await bob.post("/runs", json=body, headers={"Idempotency-Key": uuid.uuid4().hex})
        check("foreign_conversation_cannot_bind_run", r.status_code == 404)
        first = await wait(alice, first)
        report["runs"].append(first)
        check("first_read_completes", first["status"] == "SUCCEEDED")
        r = await alice.post(
            "/runs",
            json={**body, "message": "查询刚才那个订单的物流"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        r.raise_for_status()
        second = await wait(alice, r.json())
        report["runs"].append(second)
        check(
            "anaphora_reads_current_merchant_state",
            second["status"] == "SUCCEEDED" and "ORD-1001" in second["result"]["message"],
        )
        r = await alice.get(f"/conversations/{cid}")
        r.raise_for_status()
        check("ordered_durable_history", [t["seq"] for t in r.json()["turns"]] == [1, 2])
        check("second_sequence_is_monotonic", second["conversation_seq"] == 2)
        check(
            "conversation_delete", (await alice.delete(f"/conversations/{cid}")).status_code == 200
        )
        check(
            "deleted_conversation_cannot_recall",
            (await alice.get(f"/conversations/{cid}")).status_code == 404,
        )
        r = await alice.post("/runs", json=body, headers={"Idempotency-Key": uuid.uuid4().hex})
        check("deleted_conversation_cannot_bind_new_intent", r.status_code == 404)
        check(
            "task_audit_survives_conversation_delete",
            (await alice.get(f"/runs/{first['id']}")).status_code == 200,
        )
        report["status"] = "passed"
    finally:
        for c in (alice, bob, support):
            await c.aclose()
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
