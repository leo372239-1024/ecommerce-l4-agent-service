import argparse
import json
import os
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["up", "down", "status", "smoke"])
    parser.add_argument(
        "--profile", action="append", choices=["search", "observability"], default=[]
    )
    parser.add_argument(
        "--skip-tool-publish",
        action="store_true",
        help="Infrastructure/CI only: leave unpublished tool catalogs unable to admit agent tasks",
    )
    args = parser.parse_args()
    if args.skip_tool_publish and args.action != "up":
        parser.error("--skip-tool-publish 只用于 up")
    env = os.environ.copy()
    env["ELASTICSEARCH_ENABLED"] = str("search" in args.profile).lower()
    env["OTEL_ENABLED"] = str("observability" in args.profile).lower()
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
    ]
    for profile in args.profile:
        command.extend(["--profile", profile])
    if args.action == "up":
        # Build the shared API/worker/migration image first: avoid registry pulls for local-only tags.
        subprocess.run([*command, "build", "api", "web", "sandbox"], cwd=ROOT, env=env, check=True)
        command.extend(["up", "-d", "--wait", "--wait-timeout", "300"])
    elif args.action == "down":
        command.extend(["down"])  # No -v: preserve all project database volumes.
    elif args.action == "status":
        command.append("ps")
    else:
        for url in [
            "http://localhost:8000/api/v1/health/ready",
            "http://localhost:8080/api/v1/health/ready",
            "http://localhost:8080/healthz",
        ]:
            with urllib.request.urlopen(url, timeout=5) as response:
                assert response.status == 200
                body = response.read()
                if "/ready" in url:
                    assert json.loads(body)["status"] == "ok"
        print("Core HTTP and same-origin proxy smoke passed")
        return
    subprocess.run(command, cwd=ROOT, env=env, check=True)
    if args.action == "up" and not args.skip_tool_publish:
        # Database migration remains independent of cloud APIs. Index publishing is an explicit
        # deployment operation and only changed documents generate paid embeddings.
        publish = command[: command.index("up")] + [
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "app.business.index_catalog",
        ]
        subprocess.run(publish, cwd=ROOT, env=env, check=True)
    elif args.action == "up":
        print(
            "Infrastructure started; tool publishing skipped. Unpublished catalogs reject agent tasks (503)."
        )


if __name__ == "__main__":
    main()
