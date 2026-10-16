"""Exercise only this Compose project; faults always restart their own dependency."""

import argparse
import http.client
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/step3"
COMPOSE = [
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
]


def compose(*args, data=None, binary=False):
    result = subprocess.run(
        COMPOSE + list(args),
        input=data,
        capture_output=True,
        text=not binary,
        check=True,
        timeout=60,
    )
    return result.stdout


def request(path, port=8000):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def wait_ready(timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if request("/api/v1/health/ready")[0] == 200:
                return
        except (OSError, TimeoutError):
            pass
        time.sleep(1)
    raise RuntimeError("Project API did not recover readiness before the timeout")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--faults", action="store_true", help="Temporarily stop only project Redis/Postgres"
    )
    parser.add_argument("--output-dir", type=Path, default=OUT)
    args = parser.parse_args()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    report = {"captured_at": datetime.now(UTC).isoformat(), "scope": "infrastructure_only"}
    wait_ready()
    report["ready"] = json.loads(request("/api/v1/health/ready")[1])
    assert request("/api/v1/health/ready", 8080)[0] == 200
    report["same_origin_proxy"] = "passed"

    report["request_limit"] = {}
    for port in (8000, 8080):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.putrequest("POST", "/api/v1/health/live")
            connection.putheader("Content-Length", str(100 * 1024 * 1024 + 1))
            connection.endheaders()
            assert connection.getresponse().status == 413
            report["request_limit"][str(port)] = "oversize_content_length_rejected_413"
        finally:
            connection.close()

    runtime = compose(
        "exec",
        "-T",
        "api",
        "python",
        "-",
        data=(ROOT / "scripts/runtime_probe.py").read_text(encoding="utf-8"),
    )
    report["runtime_sdk"] = json.loads(runtime)
    revision = compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "agent",
        "-d",
        "agent",
        "-At",
        "-c",
        "SELECT version_num FROM alembic_version",
    ).strip()
    compose("run", "--rm", "--no-deps", "migrate")
    assert (
        revision
        == compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "agent",
            "-d",
            "agent",
            "-At",
            "-c",
            "SELECT version_num FROM alembic_version",
        ).strip()
    )
    report["migration_rerun"] = "passed"

    backup = compose(
        "exec", "-T", "postgres", "pg_dump", "-U", "agent", "-d", "agent", "-Fc", binary=True
    )
    (out / "infrastructure.dump").write_bytes(backup)
    restore_db = "infra_restore_" + uuid.uuid4().hex[:12]
    compose("exec", "-T", "postgres", "createdb", "-U", "agent", restore_db)
    try:
        compose(
            "exec",
            "-T",
            "postgres",
            "pg_restore",
            "-U",
            "agent",
            "-d",
            restore_db,
            "--exit-on-error",
            data=backup,
            binary=True,
        )
        restored = (
            compose(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "agent",
                "-d",
                restore_db,
                "-At",
                "-c",
                "SELECT version_num FROM alembic_version; SELECT count(*) FROM checkpoints; "
                "SELECT '[1,0]'::vector <=> '[1,0]'::vector;",
            )
            .strip()
            .splitlines()
        )
        assert restored[0] == revision and int(restored[1]) > 0 and restored[2] == "0"
        report["backup_restore"] = {
            "status": "passed",
            "bytes": len(backup),
            "checkpoint_rows": int(restored[1]),
        }
    finally:
        # Exact fresh database name, created above; never target an existing database.
        compose("exec", "-T", "postgres", "dropdb", "-U", "agent", restore_db)

    if args.faults:
        report["dependency_faults"] = {}
        for service in ("redis", "postgres"):
            compose("stop", service)
            try:
                status, body = request("/api/v1/health/ready")
                assert status == 503, f"{service} outage returned {status} instead of 503"
                assert request("/api/v1/health/live")[0] == 200
                failed_checks = json.loads(body)["checks"]
                expected = "postgres_schema" if service == "postgres" else service
                assert failed_checks[expected] == "unavailable"
            finally:
                start = time.monotonic()
                compose("start", service)
                wait_ready()
            report["dependency_faults"][service] = {
                "ready_during_outage": 503,
                "live_during_outage": 200,
                "ready_after_restart": 200,
                "restart_to_ready_seconds": round(time.monotonic() - start, 3),
            }
    (out / "infrastructure-verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
