"""Non-invasive resource sampling of an already running delivered soak."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from measure_capacity import resources

ROOT = Path(__file__).resolve().parents[1]


def main(args):
    directory = args.directory.resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    output = directory / "resource-timeseries.jsonl"
    assert not output.exists(), "do not overwrite prior resource observations"
    normal_names = {
        "l4-agent-" + name + "-1"
        for name in (
            "api",
            "worker",
            "merchant-mcp",
            "web",
            "postgres",
            "redis",
            "elasticsearch",
            "sandbox",
            "otel-collector",
            "jaeger",
            "prometheus",
            "grafana",
        )
    }
    started = time.monotonic()
    samples = 0
    while True:
        inventory = resources()
        sample = {
            "captured_at": datetime.now(UTC).isoformat(),
            "normal_services": [r for r in inventory if r["Name"] in normal_names],
            "benchmark_services": [
                r for r in inventory if r["Name"].startswith("l4-agent-benchmark-")
            ],
            "scope": "30-second sampled Docker container resources; excludes WSL/host/other projects; peaks may occur between samples",
        }
        with output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(sample) + "\n")
        samples += 1
        completed = (directory / "summary.json").exists()
        timed_out = time.monotonic() - started > manifest["seconds"] + 600
        if completed or timed_out:
            report = {
                "captured_at": datetime.now(UTC).isoformat(),
                "samples": samples,
                "observation_seconds": time.monotonic() - started,
                "soak_completed": completed,
                "scope": "Sampling began after the soak controller started; do not claim full start-to-end coverage or model throughput.",
            }
            (directory / "resource-observation.json").write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
            print(json.dumps(report))
            return
        time.sleep(30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    main(parser.parse_args())
