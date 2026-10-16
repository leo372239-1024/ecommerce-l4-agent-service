"""Bounded supervisor for OFFLINE history preparation timeouts; scored episodes never rerun."""

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "artifacts/benchmarks/step10-memory-confirmation-v1"
COMMAND = [
    sys.executable,
    str(ROOT / "scripts/benchmark.py"),
    "run",
    "--experiment",
    "step10-memory-confirmation-v1",
    "--variants",
    "P,M-vector",
    "--trials",
    "3",
    "--seed",
    "11001",
    "--concurrency",
    "1",
    "--dataset",
    "step10-memory-v1.json",
    "--source-snapshot",
    str(OUTPUT / "source-manifest.json"),
]


def count(output):
    return sum(1 for _ in (output / "case-results.jsonl").open(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume-after-recovered-http400", action="store_true")
    parser.add_argument("--offline-preparation-retry", action="store_true")
    parser.add_argument("--experiment", default="step10-memory-confirmation-v1")
    parser.add_argument("--probe-name", default="exact-failed-body-probe-298.json")
    args = parser.parse_args()
    assert Path(args.experiment).name == args.experiment
    assert Path(args.probe_name).name == args.probe_name
    output = ROOT / "artifacts/benchmarks" / args.experiment
    config = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert config["variants"] == ["P", "M-vector"]
    command = [
        sys.executable,
        str(ROOT / "scripts/benchmark.py"),
        "run",
        "--experiment",
        args.experiment,
        "--variants",
        "P,M-vector",
        "--trials",
        str(config["trials"]),
        "--seed",
        str(config["seed"]),
        "--concurrency",
        str(config["concurrency"]),
        "--dataset",
        config["dataset_filename"],
        "--source-snapshot",
        str(output / "source-manifest.json"),
    ]
    if args.offline_preparation_retry:
        command += [
            "--offline-preparation-retry",
            "--preparation-wrapper",
            str(output / "operational-source/offline_prepare_retry.py"),
        ]
    expected = len(config["schedule"])
    audit_file = output / "resume-audit.jsonl"
    audits = (
        [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines() if line]
        if audit_file.exists()
        else []
    )
    first_attempt = max((r["attempt"] for r in audits), default=0) + 1
    if args.resume_after_recovered_http400:
        exact_probe = output / args.probe_name
        if exact_probe.exists():
            probe = json.loads(exact_probe.read_text(encoding="utf-8"))
            latest = json.loads(
                (output / "offline-retry-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            assert probe["request_body_match"] and probe["status_code"] == 200
            assert probe["original_request_sha256"] == latest["request_body_sha256"]
            assert (
                datetime.now(UTC) - datetime.fromisoformat(probe["captured_at"])
            ).total_seconds() < 900
        else:
            assert args.experiment == "step10-memory-confirmation-v1", (
                "V2 requires exact body probe"
            )
            probe = json.loads(
                (ROOT / "artifacts/step10/final-release/embedding-recovery-probe.json").read_text(
                    encoding="utf-8"
                )
            )
            assert probe["embedding"]["status"] == "ok", "live embedding recovery probe required"
    for attempt in range(first_attempt, first_attempt + 20):
        if (output / "summary.json").exists():
            return
        interruption = output / "interruption.json"
        if interruption.exists():
            error = json.loads(interruption.read_text(encoding="utf-8"))
            transient = "dashscope transport failure: ReadTimeout" in error.get("error", "")
            recovered_400 = (
                args.resume_after_recovered_http400
                and attempt == first_attempt
                and error.get("error") == "dashscope HTTP 400"
            )
            if error.get("error_type") != "ProviderError" or not (transient or recovered_400):
                raise RuntimeError("non_transient_preparation_interruption_requires_review")
            shutil.copyfile(
                interruption, output / f"preparation-interruption-resume-{attempt}.json"
            )
        before = count(output)
        with (output / f"resume-{attempt}.log").open("w", encoding="utf-8") as log:
            result = subprocess.run(
                command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False
            )
        record = {
            "captured_at": datetime.now(UTC).isoformat(),
            "attempt": attempt,
            "exit_code": result.returncode,
            "scored_before": before,
            "scored_after": count(output),
            "protocol": "unchanged frozen source, model, schedule and scoring; offline preparation restart only, already scored rows skipped",
            "first_resume_after_live_embedding_recovery": args.resume_after_recovered_http400
            and attempt == first_attempt,
            "offline_transport_amendment": args.offline_preparation_retry,
        }
        with audit_file.open("a", encoding="utf-8") as audit:
            audit.write(json.dumps(record) + "\n")
        print(json.dumps(record), flush=True)
        if result.returncode == 0:
            assert count(output) == expected and (output / "summary.json").exists()
            return
        time.sleep(15)
    raise RuntimeError("offline_preparation_resume_limit_exhausted")


if __name__ == "__main__":
    main()
