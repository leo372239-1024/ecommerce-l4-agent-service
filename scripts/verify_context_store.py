import argparse
import json
from pathlib import Path

from verify_agent_core import ROOT, compose


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/step6")
    args = parser.parse_args()
    source = (ROOT / "scripts/context_store_probe.py").read_text(encoding="utf-8")
    report = json.loads(compose("exec", "-T", "api", "python", "-", input=source))
    assert report["status"] == "passed"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "context-store-verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report))


if __name__ == "__main__":
    main()
