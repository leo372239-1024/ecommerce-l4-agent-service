"""Known coverage regressions against the deployed real planner and MCP, not blind evals."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from verify_agent_core import ROOT, create, login, wait


async def main(args):
    client = await login("alice")
    report = {"captured_at": datetime.now(UTC).isoformat(), "cases": []}
    try:
        for message, expected in (
            (
                "查询ORD-1002的预计送达时间和承运公司",
                {"get_delivery_estimate", "get_carrier_contact"},
            ),
            (
                "查询ORD-1001的人工客服工作时间及优惠券使用门槛",
                {"get_support_hours", "get_coupon_rules"},
            ),
            (
                "查询ORD-1001的支付状态和发票类型",
                {"get_order_payment", "get_order_invoice"},
            ),
        ):
            run = await wait(client, (await create(client, message))["id"], 180)
            assert run["status"] == "SUCCEEDED", run
            response = await client.get(f"/runs/{run['id']}/events")
            response.raise_for_status()
            sequence = [
                int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")
            ]
            assert sequence == list(range(1, run["event_seq"] + 1))
            items = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            effects = {
                event["value"]["selected_tool"]
                for event in items
                if event.get("name") == "business.tool_route"
            }
            assert expected <= effects, {"expected": sorted(expected), "actual": sorted(effects)}
            entries = [e for e in run["result"]["entries"] if e["kind"] == "business_query"]
            assert len(entries) == len(expected)
            assert run["result"]["usage"]["llm_calls"] >= 1
            report["cases"].append(
                {"message": message, "expected_effects": sorted(expected), "run": run}
            )
        report["status"] = "passed"
        args.output_dir.mkdir(parents=True, exist_ok=True)
        target = args.output_dir / "query-coverage-live.json"
        assert not target.exists()
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "passed", "known_regressions": len(report["cases"])}))
    finally:
        await client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "artifacts/step10/final-release-v6"
    )
    asyncio.run(main(parser.parse_args()))
