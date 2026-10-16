"""Bounded API outage for browser SSE acceptance; always restore the project API."""

import argparse
import json
import time

from verify_agent_core import ROOT, compose


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=25)
    args = parser.parse_args()
    if not 5 <= args.seconds <= 45:
        parser.error("seconds must be 5..45")
    try:
        compose("stop", "api")
        print("Project API stopped; browser can verify auto-reconnect now.", flush=True)
        time.sleep(args.seconds)
    finally:
        compose("start", "api")
        print("Project API restored; all volumes preserved.", flush=True)
        out = ROOT / "artifacts/step8/disconnect-fault.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {"service": "l4-agent/api", "outage_seconds": args.seconds, "restored": True}
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
