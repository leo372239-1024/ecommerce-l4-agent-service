"""Inspect bound approval before explicitly submitting a revision-checked decision."""

import argparse
import asyncio
import json
import sys
from uuid import UUID

from verify_agent_core import login


async def main(args):
    client = await login(args.username)
    try:
        path = f"/approvals/{args.operation_id}" if args.operation_id else "/approvals"
        response = await client.get(path)
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
        if args.decision:
            response = await client.post(
                f"{path}/decision",
                json={"expected_revision": args.expected_revision, "decision": args.decision},
            )
            response.raise_for_status()
            print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    finally:
        await client.aclose()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", choices=["alice", "bob", "supervisor"], default="alice")
    parser.add_argument("--operation-id", type=UUID)
    parser.add_argument("--decision", choices=["confirm", "approve", "reject"])
    parser.add_argument("--expected-revision", type=int)
    options = parser.parse_args()
    if options.decision and (not options.operation_id or options.expected_revision is None):
        parser.error("决策必须指定已审查的 --operation-id 和 --expected-revision")
    asyncio.run(main(options))
