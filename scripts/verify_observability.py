"""Verify metrics, traces and provisioned dashboards using their real HTTP APIs."""

import argparse
import base64
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def get(url, headers=None):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        urllib.request.Request(url, headers=headers or {}), timeout=15
    ) as response:
        return json.load(response)


def eventually(check, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
        except (OSError, urllib.error.URLError):
            # Startup can briefly close HTTP connections before provisioning completes.
            result = None
        if result:
            return result
        time.sleep(2)
    raise RuntimeError("Observability data did not arrive before the timeout")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/step3")
    parser.add_argument("--agent-core", action="store_true")
    parser.add_argument("--business-gateway", action="store_true")
    args = parser.parse_args()
    args.agent_core = args.agent_core or args.business_gateway
    for _ in range(3):
        assert get("http://127.0.0.1:8000/api/v1/health/ready")["status"] == "ok"
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "scope": "controlled_merchant_agent"
        if args.business_gateway
        else ("read_only_agent_core" if args.agent_core else "infrastructure_only"),
    }

    def query(expression):
        result = get(
            "http://127.0.0.1:9090/api/v1/query?" + urllib.parse.urlencode({"query": expression})
        )
        assert result["status"] == "success"
        return result["data"]["result"]

    availability = eventually(lambda: query('up{job="agent-api"}') or None)
    assert float(availability[0]["value"][1]) == 1
    counters = eventually(lambda: query("agent_http_requests_total") or None)
    assert any(float(item["value"][1]) > 0 for item in counters)
    report["prometheus"] = {"scrape_up": 1, "request_counter_series": len(counters)}

    now = datetime.now(UTC)
    trace_query = urllib.parse.urlencode(
        {
            "query.service_name": "agent-api",
            "query.start_time_min": (now - timedelta(hours=1)).isoformat(),
            "query.start_time_max": now.isoformat(),
            "query.search_depth": 5,
        }
    )
    groups = eventually(
        lambda: (
            get("http://127.0.0.1:16686/api/v3/traces?" + trace_query)
            .get("result", {})
            .get("resourceSpans")
            or None
        )
    )
    trace_ids = {
        span["traceId"]
        for group in groups
        for scope in group.get("scopeSpans", [])
        for span in scope.get("spans", [])
    }
    assert trace_ids
    report["jaeger"] = {
        "service": "agent-api",
        "api_version": "v3",
        "sampled_traces": len(trace_ids),
        "status": "passed",
    }
    if args.agent_core:
        worker_query = urllib.parse.urlencode(
            {
                "query.service_name": "agent-worker",
                "query.start_time_min": (now - timedelta(hours=1)).isoformat(),
                "query.start_time_max": now.isoformat(),
                "query.search_depth": 10,
            }
        )
        worker_groups = eventually(
            lambda: (
                get("http://127.0.0.1:16686/api/v3/traces?" + worker_query)
                .get("result", {})
                .get("resourceSpans")
                or None
            )
        )
        spans = [
            span
            for group in worker_groups
            for scope in group.get("scopeSpans", [])
            for span in scope.get("spans", [])
        ]
        assert {"agent.run", "agent.tool"} <= {span["name"] for span in spans}
        assert any(
            any(attr["key"] == "agent.workflow_id" for attr in span.get("attributes", []))
            for span in spans
        )
        gauges = query("agent_runs_count")
        assert gauges and query("agent_db_metrics_available")[0]["value"][1] == "1"
        report["agent_core"] = {
            "workflow_and_tool_trace_correlation": "passed",
            "worker_spans": len(spans),
            "durable_status_metric_series": len(gauges),
        }
        if args.business_gateway:
            assert "gateway.mcp" in {span["name"] for span in spans}
            merchant_query = urllib.parse.urlencode(
                {
                    "query.service_name": "merchant-mcp",
                    "query.start_time_min": (now - timedelta(hours=1)).isoformat(),
                    "query.start_time_max": now.isoformat(),
                    "query.search_depth": 10,
                }
            )
            merchant_groups = eventually(
                lambda: (
                    get("http://127.0.0.1:16686/api/v3/traces?" + merchant_query)
                    .get("result", {})
                    .get("resourceSpans")
                    or None
                )
            )
            merchant_spans = [
                span
                for group in merchant_groups
                for scope in group.get("scopeSpans", [])
                for span in scope.get("spans", [])
                if span["name"] == "merchant.mcp.request"
            ]
            shared_traces = {span["traceId"] for span in spans if span["name"] == "gateway.mcp"} & {
                span["traceId"] for span in merchant_spans
            }
            assert shared_traces
            assert all(
                any(a["key"] == "agent.workflow_id" for a in s.get("attributes", []))
                for s in merchant_spans
            )
            operation_gauges = query("agent_business_operations_count")
            assert len(operation_gauges) == 5 and len(gauges) == 9
            assert query("agent_merchant_outbox_pending")
            latest_flows = json.loads(
                (args.output_dir / "business-flow-verification.json").read_text(encoding="utf-8")
            )
            expected_workflows = {run["id"] for run in latest_flows["runs"]}

            def workflow_ids(items):
                return {
                    attr.get("value", {}).get("stringValue")
                    for span in items
                    for attr in span.get("attributes", [])
                    if attr["key"] == "agent.workflow_id"
                }

            actual_workflows = (
                expected_workflows & workflow_ids(spans) & workflow_ids(merchant_spans)
            )
            assert actual_workflows, "No trace correlated with the latest business-flow report"
            report["business_gateway"] = {
                "gateway_mcp_shared_traces": len(shared_traces),
                "merchant_spans": len(merchant_spans),
                "operation_status_series": len(operation_gauges),
                "latest_verified_flow_workflows": len(actual_workflows),
                "status": "passed",
            }

    password = (ROOT / ".secrets/grafana_admin_password").read_text(encoding="utf-8").strip()
    authorization = base64.b64encode(("admin:" + password).encode()).decode()
    auth = {"Authorization": "Basic " + authorization}
    eventually(lambda: get("http://127.0.0.1:3001/api/health").get("database") == "ok")
    assert (
        get("http://127.0.0.1:3001/api/datasources/uid/prometheus/health", auth)["status"] == "OK"
    )
    dashboard = get(
        "http://127.0.0.1:3001/api/dashboards/uid/agent-infra",
        auth,
    )["dashboard"]
    assert len(dashboard["panels"]) == 3
    report["grafana"] = {
        "dashboard_uid": dashboard["uid"],
        "panels": len(dashboard["panels"]),
        "status": "passed",
    }

    result = subprocess.run(
        ["docker", "ps", "--filter", "label=com.docker.compose.project=l4-agent", "-q"],
        capture_output=True,
        text=True,
        check=True,
    )
    ids = result.stdout.split()
    inspect = json.loads(
        subprocess.run(
            ["docker", "inspect", *ids], capture_output=True, text=True, check=True
        ).stdout
    )
    assert all(not container["State"].get("OOMKilled") for container in inspect)
    report["containers"] = [
        {
            "name": container["Name"].lstrip("/"),
            "restart_count": container["RestartCount"],
            "oom_killed": container["State"]["OOMKilled"],
        }
        for container in inspect
    ]
    stats = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", *ids],
        capture_output=True,
        text=True,
        check=True,
    )
    report["idle_resource_snapshot"] = [json.loads(row) for row in stats.stdout.splitlines() if row]
    out = args.output_dir / "observability-verification.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "idle_resource_snapshot"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
