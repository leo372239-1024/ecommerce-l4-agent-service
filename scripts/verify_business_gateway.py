"""Host wrapper: capture actual container probe results without exposing credentials."""

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(security=False, output_dir=None):
    completed = subprocess.run(
        ["docker", "exec", "-i", "l4-agent-api-1", "python", "-"],
        input=(
            ROOT
            / (
                "scripts/business_security_probe.py"
                if security
                else "scripts/business_gateway_probe.py"
            )
        ).read_text(encoding="utf-8"),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    target = output_dir or ROOT / "artifacts/step5"
    target.mkdir(parents=True, exist_ok=True)
    if completed.returncode:
        (target / "gateway-probe-failure.txt").write_text(completed.stderr, encoding="utf-8")
        raise RuntimeError("gateway probe failed; inspect ignored local failure artifact")
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    (
        target
        / (
            "business-security-verification.json"
            if security
            else "business-gateway-verification.json"
        )
    ).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "checks": report["checks"],
                "routes": len(report.get("routes", [])),
                "correct": sum(r["correct"] for r in report.get("routes", [])),
                "arguments_correct": sum(r["argument_correct"] for r in report.get("routes", [])),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--security", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    main(args.security, args.output_dir)
