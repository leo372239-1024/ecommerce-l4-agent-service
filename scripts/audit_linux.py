"""Audit the locked Linux dependency graph in an isolated build stage and export its SBOM."""

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/step3")
    args = parser.parse_args()
    pins = dict(
        line.split("=", 1)
        for line in (ROOT / "infra/images.env").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    image = "l4-agent-dependency-audit:stage3"
    subprocess.run(
        [
            "docker",
            "build",
            "--target",
            "dependency-audit",
            "--build-arg",
            "PYTHON_IMAGE=" + pins["PYTHON_IMAGE"],
            "-t",
            image,
            str(ROOT / "backend"),
        ],
        check=True,
    )
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--entrypoint",
            "cat",
            image,
            "/tmp/linux-dependencies.cdx.json",
        ],
        capture_output=True,
        check=True,
    )
    out = args.output_dir / "linux-dependencies.cdx.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(result.stdout)
    print(f"Linux dependency audit passed; SBOM saved in {out}")


if __name__ == "__main__":
    main()
