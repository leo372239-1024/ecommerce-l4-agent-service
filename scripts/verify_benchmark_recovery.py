"""Reuse the existing real-model fault scenarios in the isolated step9 stack."""

import argparse
import asyncio
import json
from pathlib import Path

import benchmark
import verify_agent_core

ROOT = Path(__file__).resolve().parents[1]


def isolated_compose(*args, input=None):
    replacements = {
        "api": "benchmark-api",
        "worker": "benchmark-worker",
        "merchant-mcp": "benchmark-merchant",
    }
    return benchmark.compose(*(replacements.get(arg, arg) for arg in args), input=input)


async def main(args):
    output = ROOT / "artifacts/benchmarks" / args.experiment
    output.mkdir(parents=True, exist_ok=True)
    verify_agent_core.BASE = "http://localhost:8009/api/v1"
    verify_agent_core.compose = isolated_compose
    # Import AFTER redirecting the shared helpers, so all writes/faults use step9 services.
    import verify_business_flows

    isolated_compose("up", "-d", "--no-deps", "worker")
    try:
        await verify_business_flows.main(argparse.Namespace(faults=True, output_dir=output))
        code = (ROOT / "scripts/business_security_probe.py").read_text(encoding="utf-8")
        raw = isolated_compose("exec", "-T", "api", "python", "-", input=code)
        (output / "business-security.json").write_text(raw, encoding="utf-8")
    finally:
        # Normal project services and volumes are never stopped or replaced.
        isolated_compose("stop", "worker")
    print(
        json.dumps(
            {"output": str(output), "stack": "isolated_benchmark_step9", "recovery": "completed"}
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", default="step9-recovery")
    asyncio.run(main(parser.parse_args()))
