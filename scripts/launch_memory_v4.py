"""Launch the preregistered V6 confirmation once; freeze source before first inference."""

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/benchmarks/step10-memory-final-v4"


def main():
    assert not OUT.exists(), "preserve the first registered launch"
    OUT.mkdir(parents=True)
    files = [
        *ROOT.glob("backend/app/**/*.py"),
        *ROOT.glob("backend/migrations/**/*.py"),
        ROOT / "scripts/benchmark.py",
        ROOT / "scripts/benchmark_container.py",
        ROOT / "scripts/offline_prepare_retry.py",
        ROOT / "scripts/analyze_memory_v4.py",
        ROOT / "docs/evaluation/step10-memory-v4.json",
        ROOT / "docs/evaluation/step10-memory-v4-preregistration.md",
    ]
    hashes = {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(files)
    }
    assert hashes["docs/evaluation/step10-memory-v4.json"] == (
        "7639375a5fb7eec040388930d46b7e45cd4fd8d4a9a79a8434f5607613224c87"
    )
    fixture = json.loads(
        (
            ROOT
            / "artifacts/step10/final-release-v5/memory-v4-fixture-verification-before-inference.json"
        ).read_text(encoding="utf-8")
    )
    assert fixture["passed"] and fixture["cases"] == 64
    lock = {
        "captured_at": datetime.now(UTC).isoformat(),
        "source_hashes": hashes,
        "scope": "V6 application, scoring/runner, offline wrapper, analyzer and registered V4 dataset/protocol frozen before first V4 inference. No intermediate V3 quality inspected; V3 keeps its independent V5 read-only snapshot. New 60 cases primary, 4 known carrier regressions separate.",
    }
    (OUT / "launch-source-lock.json").write_text(
        json.dumps(lock, indent=2) + "\n", encoding="utf-8"
    )
    context = {
        "captured_at": datetime.now(UTC).isoformat(),
        "scope": "Operational disclosure before inference, no scoring or quality changes.",
        "concurrent_work": "V3 original snapshot may still be preparing/scoring; normal V6 release checks and then 7200-second soak may run on this same Docker host. Episode fixture IDs are UUID-isolated. Shared PostgreSQL/remote API/resource contention can affect latency; no isolated-host saturation claim. P and M-vector stay randomly interleaved at registered concurrency 2.",
    }
    (OUT / "pre-inference-operational-context.json").write_text(
        json.dumps(context, indent=2) + "\n", encoding="utf-8"
    )
    command = [
        sys.executable,
        str(ROOT / "scripts/benchmark.py"),
        "run",
        "--experiment",
        OUT.name,
        "--variants",
        "P,M-vector",
        "--trials",
        "3",
        "--seed",
        "17001",
        "--concurrency",
        "2",
        "--dataset",
        "step10-memory-v4.json",
        "--offline-preparation-retry",
    ]
    with (OUT / "initial-run.log").open("x", encoding="utf-8") as stream:
        result = subprocess.run(
            command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=False
        )
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "exit_code": result.returncode,
        "recorded_units": sum(1 for _ in (OUT / "case-results.jsonl").open(encoding="utf-8"))
        if (OUT / "case-results.jsonl").exists()
        else 0,
        "scope": "Initial registered schedule, no intermediate quality inspection.",
    }
    (OUT / "initial-lifecycle.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report), flush=True)
    if result.returncode:
        raise RuntimeError("preserve interruption; nontransient offline HTTP400 needs exact probe")


if __name__ == "__main__":
    main()
