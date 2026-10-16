"""Host entry point for isolated Docker comparisons and reproducibility manifests."""

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def compose(*args, input=None):
    result = subprocess.run(
        [
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
            "-f",
            str(ROOT / "infra/benchmark-compose.yml"),
            *args,
        ],
        input=input,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout


def source_manifest():
    files = sorted(
        [
            *ROOT.glob("backend/app/**/*.py"),
            *ROOT.glob("backend/migrations/**/*.py"),
            *ROOT.glob("scripts/*.py"),
            *ROOT.glob("backend/tests/**/*.py"),
            *ROOT.glob("frontend/src/*"),
            *ROOT.glob("sandbox/*.py"),
            *ROOT.glob("docs/evaluation/*.json"),
            *ROOT.glob("docs/evaluation/*.md"),
            *ROOT.glob("docs/evaluation/*.txt"),
            ROOT / "backend/uv.lock",
            ROOT / "frontend/package-lock.json",
            ROOT / "infra/docker-compose.yml",
            ROOT / "infra/benchmark-compose.yml",
            ROOT / "infra/images.env",
            ROOT / "backend/Dockerfile",
            ROOT / "backend/pyproject.toml",
            ROOT / "backend/alembic.ini",
            ROOT / "docs/evaluation/step9-dataset.json",
        ]
    )
    hashes = {
        str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in files
    }
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    images = json.loads(
        subprocess.run(
            [
                "docker",
                "inspect",
                "l4-agent-api-1",
                "l4-agent-worker-1",
                "l4-agent-postgres-1",
                "l4-agent-benchmark-api-1",
                "l4-agent-benchmark-merchant-1",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "git_commit": head.stdout.strip() if head.returncode == 0 else None,
        "git_note": "Uncommitted source is frozen by per-file SHA256; git commit alone is insufficient.",
        "source_hashes": hashes,
        "aggregate_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        "images": [
            {
                "name": item["Name"],
                "image_id": item["Image"],
                "cpu_limit": item["HostConfig"]["NanoCpus"],
                "memory_limit_bytes": item["HostConfig"]["Memory"],
                "safe_environment": {
                    key: value
                    for entry in item["Config"].get("Env", [])
                    for key, _, value in [entry.partition("=")]
                    if key
                    in {
                        "APP_ENV",
                        "POSTGRES_DB",
                        "REDIS_URL",
                        "ZHIPU_API_BASE",
                        "ZHIPU_MODEL",
                        "ELASTICSEARCH_ENABLED",
                        "TOOL_ROUTING_ENABLED",
                        "CONTEXT_ENGINEERING_ENABLED",
                        "OTEL_ENABLED",
                        "SANDBOX_ENABLED",
                    }
                },
            }
            for item in images
        ],
        "docker_version": subprocess.run(
            ["docker", "version", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout,
        "docker_resources": subprocess.run(
            ["docker", "info", "--format", '{"cpus":{{.NCPU}},"memory_bytes":{{.MemTotal}}}'],
            capture_output=True,
            text=True,
            check=True,
        ).stdout,
    }


def setup():
    exists = compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "agent",
        "-d",
        "agent",
        "-Atc",
        "SELECT 1 FROM pg_database WHERE datname='agent_benchmark_step9'",
    ).strip()
    if not exists:
        compose("exec", "-T", "postgres", "createdb", "-U", "agent", "agent_benchmark_step9")
    compose("run", "--rm", "--no-deps", "benchmark-init")
    compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "agent",
        "-d",
        "agent_benchmark_step9",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        "UPDATE merchant_policies SET supervisor_threshold_minor=5000 WHERE tenant_id='demo-shop'",
    )
    compose("up", "-d", "--no-deps", "benchmark-merchant", "benchmark-api")


def main(args):
    if args.command == "setup":
        setup()
        print("Dedicated benchmark database and MCP ready.")
    elif args.command == "run":
        output = ROOT / "artifacts/benchmarks" / args.experiment
        output.mkdir(parents=True, exist_ok=True)
        frozen = source_manifest()
        snapshot_origin = getattr(args, "source_snapshot", None)
        snapshot_root = ROOT
        if snapshot_origin:
            original = json.loads(Path(snapshot_origin).read_text(encoding="utf-8"))
            snapshot_root = Path(snapshot_origin).parent / "frozen-source"
            if any(
                hashlib.sha256((snapshot_root / p).read_bytes()).hexdigest() != digest
                for p, digest in original["source_hashes"].items()
            ):
                raise RuntimeError("frozen_snapshot_integrity_failed")
            frozen.update(
                source_hashes=original["source_hashes"],
                aggregate_sha256=original["aggregate_sha256"],
                source_snapshot_origin=str(snapshot_origin),
            )
        path = output / "source-manifest.json"
        if (
            path.exists()
            and json.loads(path.read_text(encoding="utf-8"))["aggregate_sha256"]
            != frozen["aggregate_sha256"]
        ):
            raise RuntimeError("source_changed_use_new_experiment")
        if path.exists():
            # Preserve first-start provenance; later inventories must not relabel old rows.
            (
                output
                / ("resume-inventory-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + ".json")
            ).write_text(json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            path.write_text(json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8")
        # Preserve public source contents as well as hashes for an uncommitted tree.
        # No credentials, environment files or secret mounts are copied.
        for relative in frozen["source_hashes"]:
            snapshot_path = output / "frozen-source" / relative
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            source_path = snapshot_root / relative
            if source_path.resolve() != snapshot_path.resolve():
                shutil.copyfile(source_path, snapshot_path)
        command = [
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            str((output / "frozen-source/backend/app").resolve()).replace("\\", "/")
            + ":/app/app:ro",
            "--volume",
            str((output / "frozen-source/scripts").resolve()).replace("\\", "/") + ":/benchmark:ro",
            "--volume",
            str((output / "frozen-source/docs/evaluation").resolve()).replace("\\", "/")
            + ":/evaluation:ro",
            "benchmark",
            "python",
            "/benchmark/benchmark_container.py",
            "--output",
            "/artifacts/benchmarks/" + args.experiment,
            "--variants",
            args.variants,
            "--trials",
            str(args.trials),
            "--seed",
            str(args.seed),
            "--concurrency",
            str(args.concurrency),
            "--soak-seconds",
            str(args.soak_seconds),
            "--dataset",
            "/evaluation/" + args.dataset,
        ]
        if snapshot_origin:
            config = json.loads(
                (Path(snapshot_origin).parent / "manifest.json").read_text(encoding="utf-8")
            )
            position = command.index("benchmark")
            command[position:position] = [
                "--env",
                "ZHIPU_API_BASE=" + config.get("llm_api_base", "https://openrouter.ai/api/v1"),
                "--env",
                "ZHIPU_MODEL=" + config["model"],
                "--env",
                "LLM_TEMPERATURE="
                + str(config.get("llm_temperature", config.get("temperature", 0.3))),
            ]
        if args.limit:
            command += ["--limit", str(args.limit)]
        if args.case_ids:
            command += ["--case-ids", args.case_ids]
        if getattr(args, "suite", "agent") == "routing":
            command = command[: command.index("python") + 1] + [
                "/benchmark/route_scale_container.py",
                "--output",
                "/artifacts/benchmarks/" + args.experiment,
                "--dataset",
                "/evaluation/" + args.dataset,
                "--trials",
                str(args.trials),
                "--seed",
                str(args.seed),
            ]
        if getattr(args, "offline_preparation_retry", False):
            if getattr(args, "suite", "agent") == "routing":
                raise RuntimeError("offline wrapper only applies to history preparation")
            wrapper_origin = Path(
                getattr(args, "preparation_wrapper", None)
                or ROOT / "scripts/offline_prepare_retry.py"
            )
            wrapper = output / "operational-source/offline_prepare_retry.py"
            wrapper.parent.mkdir(parents=True, exist_ok=True)
            if wrapper.exists() and wrapper.read_bytes() != wrapper_origin.read_bytes():
                raise RuntimeError("operational_wrapper_changed_use_new_amendment")
            if wrapper.resolve() != wrapper_origin.resolve():
                shutil.copyfile(wrapper_origin, wrapper)
            position = command.index("benchmark")
            command[position:position] = [
                "--volume",
                str(wrapper.resolve()).replace("\\", "/") + ":/offline_prepare_retry.py:ro",
                "--env",
                "OFFLINE_RETRY_LOG=/artifacts/benchmarks/"
                + args.experiment
                + "/offline-retry-events.jsonl",
            ]
            command.insert(command.index("python") + 1, "/offline_prepare_retry.py")
        # Stream progress instead of buffering a long benchmark in memory.
        base = [
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
            "-f",
            str(ROOT / "infra/benchmark-compose.yml"),
        ]
        subprocess.run(base + command, check=True)
    elif args.command == "reproduce":
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        current = source_manifest()
        changed = [
            p
            for p, digest in manifest["source_hashes"].items()
            if current["source_hashes"].get(p) != digest
        ]
        snapshot_match = None
        if args.from_snapshot:
            original_root = Path(args.manifest).parent / "frozen-source"
            snapshot_match = all(
                (original_root / p).is_file()
                and hashlib.sha256((original_root / p).read_bytes()).hexdigest() == digest
                for p, digest in manifest["source_hashes"].items()
            )
            if not snapshot_match:
                raise RuntimeError("snapshot_integrity_failed")
        print(
            json.dumps(
                {
                    "source_match": not changed,
                    "changed_files": changed,
                    "snapshot_match": snapshot_match,
                    "action": "Verification only"
                    if args.check_only
                    else "Repeat frozen experiment with fresh inference",
                },
                indent=2,
            )
        )
        if changed and not args.from_snapshot:
            raise SystemExit(1)
        if not args.check_only:
            experiment_manifest = json.loads(
                (Path(args.manifest).parent / "manifest.json").read_text(encoding="utf-8")
            )
            if (
                hashlib.sha256(
                    (
                        (original_root if args.from_snapshot else ROOT)
                        / "docs/evaluation"
                        / experiment_manifest.get("dataset_filename", "step9-dataset.json")
                    ).read_bytes()
                ).hexdigest()
                != experiment_manifest["dataset_sha256"]
            ):
                raise RuntimeError("dataset_changed")
            main(
                argparse.Namespace(
                    command="run",
                    experiment=args.experiment
                    or "step9-reproduce-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S"),
                    variants=",".join(experiment_manifest["variants"]),
                    trials=experiment_manifest["trials"],
                    seed=experiment_manifest["seed"],
                    concurrency=experiment_manifest.get("concurrency", 1),
                    soak_seconds=experiment_manifest.get("soak_seconds", 0),
                    dataset=experiment_manifest.get("dataset_filename", "step9-dataset.json"),
                    suite="routing" if experiment_manifest.get("component_only") else "agent",
                    source_snapshot=args.manifest if args.from_snapshot else None,
                    offline_preparation_retry=args.offline_preparation_retry,
                    preparation_wrapper=str(
                        Path(args.manifest).parent / "operational-source/offline_prepare_retry.py"
                    )
                    if args.offline_preparation_retry
                    else None,
                    limit=None,
                    case_ids=",".join(
                        sorted({s["case_id"] for s in experiment_manifest["schedule"]})
                    ),
                )
            )
    elif args.command == "compare":
        import sys

        sys.path.insert(0, str(ROOT / "backend"))
        from app.evaluation.oracle import compare

        rows = [
            json.loads(line) for line in Path(args.results).read_text(encoding="utf-8").splitlines()
        ]
        print(json.dumps(compare(rows, args.baseline, args.candidate), indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup")
    run = sub.add_parser("run")
    run.add_argument("--experiment", default="step9-main")
    run.add_argument("--variants", default="B0,B1,P,A-plan,A-route,A-memory,A-reflect")
    run.add_argument("--trials", type=int, default=5, choices=range(1, 6))
    run.add_argument("--dataset", default="step9-dataset.json")
    run.add_argument("--suite", choices=["agent", "routing"], default="agent")
    run.add_argument(
        "--source-snapshot", help="Verified source-manifest.json and sibling frozen-source"
    )
    run.add_argument("--seed", type=int, default=901)
    run.add_argument("--limit", type=int)
    run.add_argument("--case-ids")
    run.add_argument("--concurrency", type=int, choices=[1, 2, 4], default=2)
    run.add_argument("--soak-seconds", type=int, default=0)
    run.add_argument("--offline-preparation-retry", action="store_true")
    run.add_argument(
        "--preparation-wrapper", help="Preserve registered offline-only wrapper on resume"
    )
    rep = sub.add_parser("reproduce")
    rep.add_argument("--manifest", required=True)
    rep.add_argument("--check-only", action="store_true")
    rep.add_argument("--from-snapshot", action="store_true")
    rep.add_argument("--offline-preparation-retry", action="store_true")
    rep.add_argument("--experiment")
    comp = sub.add_parser("compare")
    comp.add_argument("--results", required=True)
    comp.add_argument("--baseline", default="B1")
    comp.add_argument("--candidate", default="P")
    main(parser.parse_args())
