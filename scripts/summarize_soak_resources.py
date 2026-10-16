"""Summarize completed sampled container resources without claiming host-wide peaks."""

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNITS = {
    "B": 1,
    "kB": 1000,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
}


def memory_bytes(value):
    amount, unit = re.fullmatch(r"([\d.]+)\s*([A-Za-z]+)", value.strip()).groups()
    return round(float(amount) * UNITS[unit])


def main(args):
    directory = args.directory.resolve()
    assert ROOT in directory.parents, "workspace-local evidence required"
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    observation = json.loads((directory / "resource-observation.json").read_text(encoding="utf-8"))
    assert summary["actual_seconds"] >= 7200 and observation["soak_completed"]
    samples = [
        json.loads(line)
        for line in (directory / "resource-timeseries.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    assert len(samples) == observation["samples"] and samples
    services, totals, container_ids = {}, [], {}
    for sample in samples:
        total = 0
        for row in sample["normal_services"]:
            name = row["Name"]
            used = memory_bytes(row["MemUsage"].split(" / ")[0])
            total += used
            record = services.setdefault(name, {"memory_bytes_peak": 0, "cpu_percent_peak": 0})
            record["memory_bytes_peak"] = max(record["memory_bytes_peak"], used)
            record["cpu_percent_peak"] = max(
                record["cpu_percent_peak"], float(row["CPUPerc"].rstrip("%"))
            )
            container_ids.setdefault(name, set()).add(row["Container"])
        totals.append({"captured_at": sample["captured_at"], "bytes": total})
    peak = max(totals, key=lambda row: row["bytes"])
    current = json.loads(
        subprocess.run(
            ["docker", "inspect", *sorted(services)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
    )
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "samples": len(samples),
        "first_sample": samples[0]["captured_at"],
        "last_sample": samples[-1]["captured_at"],
        "sample_window_seconds": (
            datetime.fromisoformat(samples[-1]["captured_at"])
            - datetime.fromisoformat(samples[0]["captured_at"])
        ).total_seconds(),
        "same_sample_normal_container_memory_peak": peak,
        "per_service": services,
        "unchanged_sampled_container_ids": all(len(ids) == 1 for ids in container_ids.values()),
        "end_inventory": {
            row["Name"].lstrip("/"): {
                "image_id": row["Image"],
                "container_id": row["Id"],
                "restart_count": row["RestartCount"],
                "oom_killed": row["State"]["OOMKilled"],
                "health": row["State"].get("Health", {}).get("Status"),
            }
            for row in current
        },
        "scope": "Normal project containers only; excludes benchmark containers, other projects and host/WSL memory. PostgreSQL is shared with benchmark work. Sampling began after soak start and can miss between-sample peaks. Per-service CPU maxima are not simultaneous, and Docker 100% CPU represents one core. Not model saturation throughput.",
    }
    output = directory / "resources-assessment.json"
    assert not output.exists(), "preserve first assessment"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"samples": len(samples), "memory_peak_gib": peak["bytes"] / 1024**3}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--directory", type=Path, default=ROOT / "artifacts/step10/live-soak-final-v6"
    )
    main(parser.parse_args())
