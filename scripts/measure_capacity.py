"""Controlled API/SSE load: no model RPS claim, no changes to demonstration orders."""

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
BASE = "http://localhost:8009/api/v1"


def resources():
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        r
        for line in result.stdout.splitlines()
        if (r := json.loads(line))["Name"].startswith("l4-agent-")
    ]


async def login(client, name):
    response = await client.post(
        "/auth/login",
        json={
            "username": name,
            "password": (ROOT / ".secrets" / f"demo_{name}_password").read_text().strip(),
        },
    )
    response.raise_for_status()
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * p))]


async def control(client, concurrency, seconds):
    stop, latencies, codes = asyncio.Event(), [], {}
    resource_samples = []
    stop_reason = None
    started = time.monotonic()
    deadline = started + seconds

    async def worker():
        nonlocal stop_reason
        while time.monotonic() < deadline and not stop.is_set():
            begin = time.monotonic()
            try:
                response = await client.get("/runs?limit=20")
                code = response.status_code
            except httpx.HTTPError:
                code = 0
            latencies.append(time.monotonic() - begin)
            codes[code] = codes.get(code, 0) + 1
            # Pre-registered stop: >1% errors after at least 100 requests, or P95 >2s.
            if len(latencies) >= 100 and (
                sum(v for k, v in codes.items() if k != 200) / len(latencies) > 0.01
                or percentile(latencies[-100:], 0.95) > 2
            ):
                stop_reason = "error_rate_above_1pct_or_recent_p95_above_2s"
                stop.set()

    async def sampler():
        nonlocal stop_reason
        while time.monotonic() < deadline and not stop.is_set():
            current = await asyncio.to_thread(resources)
            resource_samples.append(
                {"elapsed_seconds": time.monotonic() - started, "containers": current}
            )
            if any(
                float(r["MemPerc"].rstrip("%")) > 90
                for r in current
                if r["Name"] in {"l4-agent-benchmark-api-1", "l4-agent-postgres-1"}
            ):
                stop_reason = "api_or_pg_memory_above_90pct"
                stop.set()
            await asyncio.sleep(10)

    samples = asyncio.create_task(sampler())
    try:
        await asyncio.gather(*(worker() for _ in range(concurrency)))
    finally:
        samples.cancel()
        await asyncio.gather(samples, return_exceptions=True)
    elapsed = time.monotonic() - started
    return {
        "concurrency": concurrency,
        "requested_seconds": seconds,
        "elapsed_seconds": elapsed,
        "requests": len(latencies),
        "rps": len(latencies) / elapsed,
        "codes": codes,
        "p50_seconds": percentile(latencies, 0.5),
        "p95_seconds": percentile(latencies, 0.95),
        "p99_seconds": percentile(latencies, 0.99),
        "stopped_at_threshold": stop.is_set(),
        "stop_reason": stop_reason,
        "resource_samples": resource_samples,
        "latencies_seconds": latencies,
    }


async def sse(client, run_id, concurrency, seconds):
    deadline, streams, errors = time.monotonic() + seconds, [], []

    async def connection(index):
        cursor, count = 0, 0
        while time.monotonic() < deadline:
            try:
                async with client.stream(
                    "GET", f"/runs/{run_id}/events", headers={"Last-Event-ID": str(cursor)}
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("id: "):
                            seq = int(line[4:])
                            if seq <= cursor:
                                errors.append("duplicate_or_reversed_seq")
                            if seq != cursor + 1:
                                errors.append("gap")
                            cursor, count = seq, count + 1
                        if index % 10 == 0:
                            await asyncio.sleep(0.1)
                        if time.monotonic() >= deadline:
                            break
            except httpx.HTTPError as exc:
                errors.append(type(exc).__name__)
                break
        streams.append({"connection": index, "events": count, "last_seq": cursor})

    await asyncio.gather(*(connection(i) for i in range(concurrency)))
    return {
        "concurrency": concurrency,
        "seconds": seconds,
        "errors": errors,
        "streams": streams,
        "slow_client_fraction": 0.1,
        "reconnect": True,
    }


async def main(args):
    output = ROOT / "artifacts/benchmarks" / args.experiment
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "endpoint": "/runs?limit=20",
        "model_involvement": "none; control API only",
        "api": [],
        "sse": [],
        "resources": [],
    }
    async with httpx.AsyncClient(
        base_url=BASE,
        timeout=35,
        limits=httpx.Limits(max_connections=220, max_keepalive_connections=100),
        trust_env=False,
    ) as client:
        await login(client, "alice")
        # Warm-up is excluded from reported stable intervals.
        await control(client, 1, args.warmup)
        for concurrency in [1, 10, 50, 100]:
            report["resources"].append(await asyncio.to_thread(resources))
            result = await control(client, concurrency, args.seconds)
            report["api"].append(result)
            (output / "capacity.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(
                json.dumps({k: v for k, v in result.items() if k != "latencies_seconds"}),
                flush=True,
            )
            if result["stopped_at_threshold"]:
                break
        response = await client.post(
            "/runs",
            json={"message": "控制API/SSE容量观察任务", "dataset_partition": "evaluation"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        response.raise_for_status()
        run = response.json()
        try:
            for concurrency in [10, 50, 100, 200]:
                result = await sse(client, run["id"], concurrency, args.sse_seconds)
                report["sse"].append(result)
                report["resources"].append(await asyncio.to_thread(resources))
                print(json.dumps({k: v for k, v in result.items() if k != "streams"}), flush=True)
                if result["errors"]:
                    break
            async with httpx.AsyncClient(base_url=BASE, timeout=10, trust_env=False) as bob:
                await login(bob, "bob")
                report["sse_foreign_owner_status"] = (
                    await bob.get(f"/runs/{run['id']}/events")
                ).status_code
        finally:
            current = (await client.get(f"/runs/{run['id']}")).json()
            cancelled = await client.post(
                f"/runs/{run['id']}/cancel", json={"expected_revision": current["revision"]}
            )
            cancelled.raise_for_status()
        report["resources"].append(await asyncio.to_thread(resources))
    (output / "capacity.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", default="step9-capacity")
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--sse-seconds", type=int, default=60)
    asyncio.run(main(parser.parse_args()))
