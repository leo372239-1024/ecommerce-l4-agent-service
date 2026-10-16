"""Durable local release checks; stop on failure and preserve every command log."""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/step10/final-release-v6"


def main():
    assert (
        json.loads((OUT / "test-verification.json").read_text(encoding="utf-8"))["linux"][
            "exit_code"
        ]
        == 0
    )
    lifecycle = OUT / "release-checks.jsonl"
    assert not lifecycle.exists(), "preserve previous checks"
    commands = [
        ["scripts/verify_business_flows.py", "--faults", "--output-dir", str(OUT)],
        ["scripts/verify_business_queries.py", "--output-dir", str(OUT)],
        ["scripts/verify_query_coverage_live.py", "--output-dir", str(OUT)],
        ["scripts/verify_final_knowledge.py", "--output-dir", str(OUT)],
        ["scripts/verify_observability.py", "--output-dir", str(OUT)],
        ["scripts/verify_telemetry_privacy.py", "--output-dir", str(OUT)],
        ["scripts/stack.py", "smoke"],
    ]
    for index, args in enumerate(commands, start=1):
        started = datetime.now(UTC).isoformat()
        path = OUT / f"release-check-{index}.log"
        with path.open("x", encoding="utf-8") as log:
            result = subprocess.run(
                [sys.executable, *args], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False
            )
        row = {
            "started_at": started,
            "ended_at": datetime.now(UTC).isoformat(),
            "command": args,
            "log": path.name,
            "exit_code": result.returncode,
        }
        with lifecycle.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)
        if result.returncode:
            raise RuntimeError("release_check_failed; preserve log and investigate")
    (OUT / "release-checks-complete.json").write_text(
        json.dumps({"captured_at": datetime.now(UTC).isoformat(), "checks": len(commands)}) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
