"""Wait for the preceding schedule, verify final source lock, then launch registered V3."""

import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "artifacts/benchmarks/step10-memory-final-v3"


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    lock_path = OUTPUT / "launch-source-lock.json"
    names = [
        *ROOT.glob("backend/app/**/*.py"),
        *ROOT.glob("backend/migrations/**/*.py"),
        ROOT / "scripts/benchmark.py",
        ROOT / "scripts/benchmark_container.py",
        ROOT / "scripts/offline_prepare_retry.py",
        ROOT / "docs/evaluation/step10-memory-v3.json",
        ROOT / "docs/evaluation/step10-memory-final-v3-preregistration.md",
    ]
    hashes = {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(names)
    }
    if lock_path.exists():
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        assert lock["source_hashes"] == hashes, "source changed after final registration"
    else:
        lock_path.write_text(
            json.dumps(
                {
                    "captured_at": datetime.now(UTC).isoformat(),
                    "source_hashes": hashes,
                    "scope": "Application, migration, runner/scoring, registered offline wrapper, new dataset and protocol locked before V2 outcome analysis; no quality-based tuning.",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    previous = ROOT / "artifacts/benchmarks/step10-memory-confirmation-v2"
    while not (previous / "summary.json").exists():
        print(
            json.dumps(
                {
                    "phase": "waiting_for_v2_completion",
                    "captured_at": datetime.now(UTC).isoformat(),
                    "recorded_v2_units": sum(
                        1 for _ in (previous / "case-results.jsonl").open(encoding="utf-8")
                    ),
                }
            ),
            flush=True,
        )
        time.sleep(45)
    assert hashes == {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in hashes
    }
    if (OUTPUT / "summary.json").exists():
        print("Registered V3 already complete; not rerunning.")
        return
    assert not (OUTPUT / "case-results.jsonl").exists(), (
        "resume existing V3 using audited supervisor"
    )
    command = [
        sys.executable,
        str(ROOT / "scripts/benchmark.py"),
        "run",
        "--experiment",
        OUTPUT.name,
        "--variants",
        "P,M-vector",
        "--trials",
        "3",
        "--seed",
        "16001",
        "--concurrency",
        "1",
        "--dataset",
        "step10-memory-v3.json",
        "--offline-preparation-retry",
    ]
    with (OUTPUT / "initial-run.log").open("x", encoding="utf-8") as stream:
        result = subprocess.run(
            command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=False
        )
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "exit_code": result.returncode,
        "recorded_units": sum(1 for _ in (OUTPUT / "case-results.jsonl").open(encoding="utf-8")),
        "scope": "Full initial registered schedule; no intermediate quality inspection.",
    }
    (OUTPUT / "initial-lifecycle.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report), flush=True)
    if result.returncode:
        raise RuntimeError(
            "V3 interrupted: inspect preparation failure, preserve prefix; HTTP400 requires exact diagnostic, never blind retry"
        )


if __name__ == "__main__":
    main()
