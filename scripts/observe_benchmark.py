"""Observe a long real-model run; sampling never generates fake agent throughput."""

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from measure_capacity import resources

ROOT = Path(__file__).resolve().parents[1]


def main(args):
    output = ROOT / "artifacts/benchmarks" / args.experiment
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    while True:
        sample = {
            "captured_at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": time.monotonic() - started,
            "resources": resources(),
        }
        active = subprocess.run(
            ["docker", "ps", "--filter", "name=l4-agent-benchmark-run", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        results = output / "case-results.jsonl"
        sample.update(
            running_containers=active,
            completed_episodes=sum(1 for _ in results.open(encoding="utf-8"))
            if results.exists()
            else 0,
        )
        with (output / "resource-timeseries.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(sample) + "\n")
        if time.monotonic() - started >= args.seconds and not active:
            break
        if not active and sample["completed_episodes"] and (output / "summary.json").exists():
            # The duration is reported honestly; do not claim 2h when the workload ended early.
            break
        time.sleep(min(args.interval, 60))
    (output / "soak-duration.json").write_text(
        json.dumps(
            {
                "observed_seconds": time.monotonic() - started,
                "requested_seconds": args.seconds,
                "two_hour_gate": time.monotonic() - started >= 7200,
                "workload": "The actual randomized real-model episode schedule; no idle samples count as extra tasks.",
            },
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", default="step9-main")
    parser.add_argument("--seconds", type=int, default=7200)
    parser.add_argument("--interval", type=int, default=10)
    main(parser.parse_args())
