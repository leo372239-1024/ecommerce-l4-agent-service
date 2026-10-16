"""Paced real model tasks through the delivered API/worker; separate from quality samples."""

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
TASKS = [
    ("查询ORD-1002的订单状态和当前物流，仅查询。", {"get_order", "get_logistics"}),
    (
        "查询ORD-1002的物流轨迹节点和预计送达时间，仅查询。",
        {"get_shipment_tracking", "get_delivery_estimate"},
    ),
    ("查询ORD-1001的发票类型，仅查询。", {"get_order_invoice"}),
    ("查询ORD-1001的退款余额，仅查询。", {"get_refund_balance"}),
]
CONVERSATION_TASKS = [
    ("查询ORD-1002的订单状态和当前物流，仅查询。", {"get_order", "get_logistics"}, "ORD-1002"),
    (
        "查询刚才那个订单的物流轨迹节点和预计送达时间，仅查询。",
        {"get_shipment_tracking", "get_delivery_estimate"},
        "ORD-1002",
    ),
    ("查询ORD-1001的发票类型，仅查询。", {"get_order_invoice"}, "ORD-1001"),
    ("查询第一个订单的退款余额，仅查询。", {"get_refund_balance"}, "ORD-1002"),
]


def deployment_inventory():
    services = ["api", "worker", "merchant-mcp", "web"]
    items = json.loads(
        subprocess.run(
            ["docker", "inspect", *[f"l4-agent-{name}-1" for name in services]],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "services": {
            item["Name"].lstrip("/"): {
                "image_id": item["Image"],
                "container_id": item["Id"],
                "started_at": item["State"]["StartedAt"],
                "restart_count": item["RestartCount"],
                "oom_killed": item["State"]["OOMKilled"],
                "health": item["State"].get("Health", {}).get("Status"),
            }
            for item in items
        },
    }


async def main(args):
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    assert not (output / "manifest.json").exists(), (
        "use a fresh output directory; do not overwrite a run"
    )
    manifest = {
        "started_at": datetime.now(UTC).isoformat(),
        "seconds": args.seconds,
        "arrival_interval_seconds": args.interval,
        "scope": "delivered normal API/worker/merchant, real OpenRouter inference, simulation read-only business; not quality confirmation or saturation throughput",
        "partition": "evaluation; no episodic/AWM learning",
        "tasks": [(m, sorted(t)) for m, t in TASKS]
        if not args.with_conversation
        else [(m, sorted(t), o) for m, t, o in CONVERSATION_TASKS],
        "with_conversation": args.with_conversation,
        "completion_rule": "actual run lasting requested duration with paced real task arrivals; report every failure; never count idle timer as tasks",
        "deployment_at_start": deployment_inventory(),
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    rows = []
    started = time.monotonic()
    async with httpx.AsyncClient(
        base_url="http://localhost:8080/api/v1", trust_env=False, timeout=15
    ) as client:
        response = await client.post(
            "/auth/login",
            json={
                "username": "alice",
                "password": (ROOT / ".secrets/demo_alice_password")
                .read_text(encoding="utf-8")
                .strip(),
            },
        )
        response.raise_for_status()
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        conversation_id = None
        if args.with_conversation:
            conversation = await client.post("/conversations", json={})
            conversation.raise_for_status()
            conversation_id = conversation.json()["id"]
        while time.monotonic() - started < args.seconds:
            begin = time.monotonic()
            expected_order = None
            if args.with_conversation:
                message, tools, expected_order = CONVERSATION_TASKS[
                    len(rows) % len(CONVERSATION_TASKS)
                ]
            else:
                message, tools = TASKS[len(rows) % len(TASKS)]
            record = {
                "arrival_at": datetime.now(UTC).isoformat(),
                "sequence": len(rows),
                "message": message,
            }
            try:
                response = await client.post(
                    "/runs",
                    json={
                        "message": message,
                        "dataset_partition": "evaluation",
                        **({"conversation_id": conversation_id} if conversation_id else {}),
                    },
                    headers={"Idempotency-Key": uuid.uuid4().hex},
                )
                response.raise_for_status()
                record["workflow_id"] = response.json()["id"]
                run = response.json()
                deadline = begin + 195
                while run["status"] in {"QUEUED", "RUNNING"} and time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
                    response = await client.get("/runs/" + record["workflow_id"])
                    response.raise_for_status()
                    run = response.json()
                kinds = {entry["kind"] for entry in (run.get("result") or {}).get("entries", [])}
                replay = await client.get("/runs/" + record["workflow_id"] + "/events")
                replay.raise_for_status()
                event_ids = [
                    int(line[4:]) for line in replay.text.splitlines() if line.startswith("id: ")
                ]
                events = [
                    json.loads(line[6:])
                    for line in replay.text.splitlines()
                    if line.startswith("data: ")
                ]
                selected_tools = {
                    e.get("value", {}).get("selected_tool")
                    for e in events
                    if e.get("name") == "business.tool_route"
                }
                selected_tools.discard(None)
                kinds |= selected_tools
                record["event_replay_contiguous"] = event_ids == list(
                    range(1, run["event_seq"] + 1)
                )
                record.update(
                    status=run["status"],
                    event_seq=run["event_seq"],
                    observed_tools=sorted(kinds),
                    usage=(run.get("result") or {}).get("usage", {}),
                )
                record["conversation_id"] = run.get("conversation_id")
                record["conversation_seq"] = run.get("conversation_seq")
                actual_orders = {
                    entry["order_id"]
                    for entry in (run.get("result") or {}).get("entries", [])
                    if entry.get("order_id")
                }
                record["order_binding_valid"] = expected_order is None or actual_orders == {
                    expected_order
                }
                record["passed"] = (
                    run["status"] == "SUCCEEDED"
                    and tools <= kinds
                    and record["usage"].get("llm_calls", 0) >= 1
                    and record["event_replay_contiguous"]
                    and record["order_binding_valid"]
                    and (not conversation_id or run.get("conversation_id") == conversation_id)
                )
                if run["status"] in {"QUEUED", "RUNNING", "WAITING_USER", "WAITING_APPROVAL"}:
                    cancellation = await client.post(
                        "/runs/" + record["workflow_id"] + "/cancel",
                        json={"expected_revision": run["revision"]},
                    )
                    record["cleanup_cancel_status"] = cancellation.status_code
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                record.update(passed=False, error_type=type(exc).__name__)
            record["elapsed_seconds"] = time.monotonic() - begin
            rows.append(record)
            with (output / "arrivals.jsonl").open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(json.dumps({k: v for k, v in record.items() if k != "message"}), flush=True)
            remaining = args.interval - (time.monotonic() - begin)
            if remaining > 0:
                await asyncio.sleep(
                    min(remaining, max(0, args.seconds - (time.monotonic() - started)))
                )
    summary = {
        "ended_at": datetime.now(UTC).isoformat(),
        "actual_seconds": time.monotonic() - started,
        "arrivals": len(rows),
        "passed": sum(r["passed"] for r in rows),
        "failed": sum(not r["passed"] for r in rows),
        "model_calls_reported": sum(r.get("usage", {}).get("llm_calls", 0) for r in rows),
        "model_tokens_reported": sum(r.get("usage", {}).get("total_tokens", 0) for r in rows),
        "included_in_quality_scores": False,
        "deployment_at_end": deployment_inventory(),
    }
    before = manifest["deployment_at_start"]["services"]
    after = summary["deployment_at_end"]["services"]
    summary["unchanged_deployment"] = before == after
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=7200)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--with-conversation", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/step10/live-soak")
    asyncio.run(main(parser.parse_args()))
