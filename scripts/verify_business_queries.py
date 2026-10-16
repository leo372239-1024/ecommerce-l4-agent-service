"""Real planner-to-MCP query execution and missing-entity resume; development regressions."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from verify_agent_core import ROOT, create, login, wait


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/step5")
    args = parser.parse_args()
    client = await login("alice")
    report = {"captured_at": datetime.now(UTC).isoformat(), "cases": []}
    try:
        for missing in (False, True):
            message = (
                "查询发票类型和物流轨迹节点" if missing else "查询ORD-1001的发票类型和物流轨迹节点"
            )
            run = await wait(client, (await create(client, message))["id"], 150)
            if missing:
                assert run["status"] == "WAITING_USER", run
                response = await client.post(
                    f"/runs/{run['id']}/resume",
                    json={
                        "expected_revision": run["revision"],
                        "input": {"order_ids": ["ORD-1001"]},
                    },
                )
                response.raise_for_status()
                run = await wait(client, run["id"], 150)
            assert run["status"] == "SUCCEEDED", run
            entries = [e for e in run["result"]["entries"] if e["kind"] == "business_query"]
            assert len(entries) == 2 and all(e["order_id"] == "ORD-1001" for e in entries)
            response = await client.get(f"/runs/{run['id']}/events")
            response.raise_for_status()
            seq = [int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")]
            assert seq == list(range(1, run["event_seq"] + 1))
            events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            routes = [e["value"] for e in events if e.get("name") == "business.tool_route"]
            assert {r["selected_tool"] for r in routes} == {
                "get_order",
                "get_order_invoice",
                "get_shipment_tracking",
            }
            assert sum(e["type"] == "RUN_FINISHED" for e in events) == (2 if missing else 1)
            report["cases"].append(
                {
                    "name": "query_clarification_resume" if missing else "two_subgoal_query",
                    "status": run["status"],
                    "workflow_id": run["id"],
                    "usage": run["result"]["usage"],
                    "routes": routes,
                    "event_count": len(seq),
                }
            )
        args.output_dir.mkdir(parents=True, exist_ok=True)
        target = args.output_dir / "business-query-verification.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"cases": len(report["cases"]), "status": "passed"}))
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
