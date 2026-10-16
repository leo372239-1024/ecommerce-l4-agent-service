"""Read-only completed schedules, snapshot and resume-prefix integrity verification."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS = {
    "step10-openrouter-confirmation-v1": 1110,
    "step10-routing-confirmation-v1": 900,
    "step10-memory-confirmation-v1": 300,
    "step10-recovery-confirmation-v1": 348,
    "step10-memory-confirmation-v2": 360,
    "step10-memory-final-v3": 360,
}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main(args):
    experiments = []
    prefixes = []
    registered = dict(EXPERIMENTS)
    if args.release == "v6":
        registered["step10-memory-final-v4"] = 384
    for name, expected in registered.items():
        directory = ROOT / "artifacts/benchmarks" / name
        marker = "complete.json" if "routing" in name else "summary.json"
        assert (directory / marker).exists(), f"incomplete experiment: {name}"
        rows = [
            json.loads(line)
            for line in (directory / "case-results.jsonl").read_text(encoding="utf-8").splitlines()
            if line
        ]
        schedule = read(directory / "manifest.json")["schedule"]
        fields = ("case_id", "variant", "trial") + (("catalog_size",) if "routing" in name else ())
        row_key = lambda row, fields=fields: tuple(row[field] for field in fields)
        assert len(rows) == len({row_key(row) for row in rows}) == expected
        assert {row_key(row) for row in rows} == {row_key(row) for row in schedule}
        source = read(directory / "source-manifest.json")
        for relative, digest in source["source_hashes"].items():
            content = (directory / "frozen-source" / relative).read_bytes()
            assert hashlib.sha256(content).hexdigest() == digest, relative
        experiments.append(
            {
                "experiment": name,
                "units": len(rows),
                "unique_schedule": "passed",
                "original_snapshot_sha256": "passed",
                "raw_results_sha256": hashlib.sha256(
                    (directory / "case-results.jsonl").read_bytes()
                ).hexdigest(),
            }
        )
        for record_path in directory.glob("operational-*.json"):
            record = read(record_path)
            recorded = record.get("prefix_files", record.get("files", {}))
            for relative, metadata in recorded.items():
                if not isinstance(metadata, dict) or not {"bytes", "sha256"} <= metadata.keys():
                    continue
                content = (directory / relative).read_bytes()[: metadata["bytes"]]
                assert len(content) == metadata["bytes"]
                assert hashlib.sha256(content).hexdigest() == metadata["sha256"], record_path
                prefixes.append(
                    {
                        "record": record_path.relative_to(ROOT).as_posix(),
                        "file": relative,
                        "bytes": len(content),
                        "original_prefix_sha256": "passed",
                    }
                )
    lock_path = ROOT / "artifacts/benchmarks/step10-memory-final-v3/launch-source-lock.json"
    lock = read(lock_path)
    checkpoint = read(ROOT / "artifacts/step10/final-release-v5/source-transition-checkpoint.json")
    assert checkpoint["status"] == "passed_before_source_transition"
    assert checkpoint["source_hashes"] == lock["source_hashes"]
    for relative, digest in lock["source_hashes"].items():
        frozen = lock_path.parent / "frozen-source" / relative
        assert hashlib.sha256(frozen.read_bytes()).hexdigest() == digest, relative
    if args.release == "v6":
        final_lock = read(
            ROOT / "artifacts/benchmarks/step10-memory-final-v4/launch-source-lock.json"
        )
        for relative, digest in final_lock["source_hashes"].items():
            assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest, relative
    default_soak_name = (
        "live-soak-final-v6" if args.release == "v6" else "live-soak-final-v5-complete"
    )
    soak = (
        args.soak_directory.resolve()
        if args.soak_directory
        else ROOT / "artifacts/step10" / default_soak_name
    )
    assert ROOT in soak.parents, "workspace-local soak evidence required"
    manifest = read(soak / "manifest.json")
    summary = read(soak / "summary.json")
    assert manifest["seconds"] == 7200 and summary["actual_seconds"] >= 7200
    assert summary["unchanged_deployment"]
    assert manifest["deployment_at_start"]["services"] == summary["deployment_at_end"]["services"]
    assert all(
        not row["oom_killed"] and row["restart_count"] == 0
        for row in summary["deployment_at_end"]["services"].values()
    )
    observation = read(soak / "resource-observation.json")
    samples = [
        json.loads(line)
        for line in (soak / "resource-timeseries.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    resources_assessment = read(soak / "resources-assessment.json")
    assert observation["soak_completed"] and observation["samples"] == len(samples) > 0
    assert resources_assessment["samples"] == len(samples)
    assert resources_assessment["unchanged_sampled_container_ids"]
    release_dir = ROOT / "artifacts/step10/final-release-v6"
    release_checks = [
        json.loads(line)
        for line in (release_dir / "release-checks.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(release_checks) == 7 and all(row["exit_code"] == 0 for row in release_checks)
    assert read(release_dir / "release-checks-complete.json")["checks"] == 7
    deployment_audit = read(release_dir / "source-manifest.json")
    assert deployment_audit["secret_scan"] == "passed"
    assert deployment_audit["local_document_links"] == "passed"
    assert all(
        deployment_audit["deployed"][service]["source_files_verified"] == 74
        for service in ("api", "worker", "merchant-mcp")
    )
    assert deployment_audit["deployed"]["web"]["served_assets_verified"] == 5
    browser_acceptance = read(release_dir / "browser-acceptance-v6.json")
    assert browser_acceptance["status"] == "passed"
    assert len(browser_acceptance["checks"]) >= 2
    assert all(
        check["status"] == "SUCCEEDED" and check["passed"] for check in browser_acceptance["checks"]
    )
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "status": "passed",
        "experiments": experiments,
        "total_units": sum(row["units"] for row in experiments),
        "resume_prefixes": prefixes,
        "final_application_protocol_source_lock": "passed",
        "v3_source_transition_checkpoint": "passed; V3 retained original read-only snapshot",
        "completed_soak_duration_and_deployment": "passed",
        "soak_directory": soak.relative_to(ROOT).as_posix(),
        "resource_sampling_and_assessment": "passed",
        "live_release_checks": len(release_checks),
        "deployment_source_and_asset_audit": "passed",
        "browser_acceptance_checks": len(browser_acceptance["checks"]),
        "scope": "Integrity verification, not an assertion that all quality targets passed; repeated trials are not independent scenarios; failed arrivals remain included.",
    }
    output = (
        ROOT / "artifacts/step10" / ("final-release-" + args.release) / "evidence-integrity.json"
    )
    assert not output.exists(), "preserve the first completed evidence audit"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "total_units": report["total_units"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", choices=["v5", "v6"], default="v6")
    parser.add_argument(
        "--soak-directory",
        type=Path,
        help="Completed immutable soak output; defaults to the release's original directory",
    )
    main(parser.parse_args())
