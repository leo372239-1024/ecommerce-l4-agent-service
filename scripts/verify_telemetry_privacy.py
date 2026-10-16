"""A harmless query canary must reach the API trace without reaching logs or exported data."""

import argparse
import base64
import json
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/step7")
    args = parser.parse_args()
    started = datetime.now(UTC)
    trace_id = uuid.uuid4().hex
    canary = "privacy-canary-" + uuid.uuid4().hex
    req = Request(
        "http://localhost:8080/api/v1/health/live?" + urlencode({"code": canary, "state": canary}),
        headers={"traceparent": "00-" + trace_id + "-" + uuid.uuid4().hex[:16] + "-01"},
    )
    with urlopen(req, timeout=5) as response:
        assert response.status == 200 and response.headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    expected = {trace_id, base64.b64encode(bytes.fromhex(trace_id)).decode()}
    spans = []
    for _ in range(40):
        query = urlencode(
            {
                "query.service_name": "agent-api",
                "query.start_time_min": (started - timedelta(seconds=2)).isoformat(),
                "query.start_time_max": datetime.now(UTC).isoformat(),
                "query.search_depth": 50,
            }
        )
        try:
            with urlopen("http://localhost:16686/api/v3/traces?" + query, timeout=5) as response:
                data = json.load(response)
        except HTTPError as exc:
            if exc.code != 404:
                raise
            time.sleep(0.5)
            continue
        groups = data.get("result", {}).get("resourceSpans", [])
        spans = [
            s
            for g in groups
            for scope in g.get("scopeSpans", [])
            for s in scope.get("spans", [])
            if s["traceId"] in expected
        ]
        if spans:
            assert canary not in json.dumps(groups), "query credential reached trace export"
            break
        time.sleep(0.5)
    assert spans, "canary request trace not found"
    for service in ("api", "web"):
        result = subprocess.run(
            ["docker", "logs", "--since", started.isoformat(), "l4-agent-" + service + "-1"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        assert canary not in result.stdout + result.stderr, (
            "query credential reached proxy/application logs"
        )
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "query_and_header_privacy": "passed",
        "proxy_log_privacy": "passed",
        "trace_found": True,
        "matched_spans": len(spans),
        "security_headers": "passed",
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "telemetry-privacy-verification.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report))


if __name__ == "__main__":
    main()
