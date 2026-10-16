"""Review the real model agent and bound proposal without printing local credentials."""

import argparse
import asyncio
import json
import sys
from uuid import UUID

from verify_agent_core import TERMINAL, create, login, wait


async def main(args):
    client = await login(args.username)
    try:
        if args.run_id:
            response = await client.get(f"/runs/{args.run_id}")
            response.raise_for_status()
            run = response.json()
        else:
            run = await create(client, args.message)
        print("workflow_id:", run["id"])
        if args.order_ids and run["status"] == "WAITING_USER":
            response = await client.post(
                f"/runs/{run['id']}/resume",
                json={"expected_revision": run["revision"], "input": {"order_ids": args.order_ids}},
            )
            response.raise_for_status()
        if args.stream:
            cursor = 0
            while True:
                async with client.stream(
                    "GET", f"/runs/{run['id']}/events", headers={"Last-Event-ID": str(cursor)}
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("id: "):
                            cursor = int(line[4:])
                        elif line.startswith("data: "):
                            event = json.loads(line[6:])
                            print(
                                cursor,
                                event["type"],
                                event.get(
                                    "stepName", event.get("name", event.get("toolCallName", ""))
                                ),
                            )
                response = await client.get(f"/runs/{run['id']}")
                response.raise_for_status()
                final = response.json()
                if final["status"] in TERMINAL:
                    break
        else:
            final = await wait(client, run["id"], 200)
        print("status:", final["status"])
        print(final["result"].get("message", final["result"].get("question", "")))
        if final["status"] == "WAITING_APPROVAL":
            print(json.dumps(final["result"]["proposal"], ensure_ascii=False, indent=2))
            print(
                "通过 /api/v1/approvals/{operation_id}/decision 提交 confirm；需主管审批时由 supervisor 提交 approve。"
            )
        if final["status"] == "WAITING_USER":
            print("使用 --run-id 上述ID --order-ids ORD-1001 恢复同一任务。")
    finally:
        await client.aclose()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", choices=["alice", "bob"], default="alice")
    parser.add_argument("--message")
    parser.add_argument("--run-id", type=UUID)
    parser.add_argument("--order-ids", nargs="+")
    parser.add_argument("--stream", action="store_true")
    options = parser.parse_args()
    if not options.message and not options.run_id:
        parser.error("需要 --message 或 --run-id")
    asyncio.run(main(options))
