"""Explicit billable probes inside the API container; export only safe metadata."""

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--read-existing", action="store_true", help="Export the last probe without new model calls"
    )
    args = parser.parse_args()
    command = [
        "docker",
        "compose",
        "--project-name",
        "l4-agent",
        "--env-file",
        str(ROOT / ".env"),
        "--env-file",
        str(ROOT / "infra/images.env"),
        "-f",
        str(ROOT / "infra/docker-compose.yml"),
        "exec",
        "-T",
        "api",
    ]
    if not args.read_existing:
        subprocess.run(
            command + ["python", "-", "--only", "all", "--output", "/tmp/provider-probe.json"],
            input=(ROOT / "scripts/provider_probe.py").read_text(encoding="utf-8"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=120,
        )
    result = subprocess.run(
        command + ["cat", "/tmp/provider-probe.json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    report = json.loads(result.stdout)
    assert all(report[name]["status"] == "ok" for name in ("embedding", "rerank", "llm"))
    report["execution_environment"] = "api_container"
    out = ROOT / "artifacts/step3/provider-probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
