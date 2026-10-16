"""Local source/evidence bundle, excluding credentials, dependencies, dumps and unrelated data."""

import argparse
import hashlib
import json
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS = {
    "step10-openrouter-confirmation-v1": "summary.json",
    "step10-routing-confirmation-v1": "complete.json",
    "step10-memory-confirmation-v1": "summary.json",
    "step10-recovery-confirmation-v1": "summary.json",
    "step10-memory-confirmation-v2": "summary.json",
    "step10-memory-final-v3": "summary.json",
    "step10-memory-final-v4": "summary.json",
}


def main(args):
    output = args.output.resolve()
    assert ROOT in output.parents and not output.exists(), "fresh workspace-local bundle required"
    release = ROOT / "artifacts/step10/final-release-v6"
    assert (release / "source-manifest.json").exists(), "final source verification required"
    integrity = json.loads((release / "evidence-integrity.json").read_text(encoding="utf-8"))
    assert integrity["status"] == "passed", "final evidence integrity verification required"
    soak = args.soak_directory.resolve()
    assert ROOT in soak.parents, "workspace-local soak evidence required"
    soak_summary = json.loads((soak / "summary.json").read_text(encoding="utf-8"))
    assert soak_summary["actual_seconds"] >= 7200, "full duration required"
    assert soak_summary["unchanged_deployment"], "same deployment required"
    assert (ROOT / "artifacts/benchmarks/step10-analysis-memory-final-v3.json").exists()
    assert (ROOT / "artifacts/benchmarks/step10-analysis-memory-final-v4.json").exists()
    assert (release / "evidence-integrity.json").exists()
    source = (
        subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        )
        .stdout.decode("utf-8")
        .split("\0")
    )
    paths = {ROOT / name for name in source if name and (ROOT / name).is_file()}
    for name, marker in EXPERIMENTS.items():
        directory = ROOT / "artifacts/benchmarks" / name
        assert (directory / marker).exists(), "incomplete registered experiment"
        for pattern in (
            "*.json",
            "*.jsonl",
            "*.md",
            "*.log",
            "*.py",
            "frozen-source/**/*",
            "operational-source/*.py",
        ):
            paths.update(p for p in directory.glob(pattern) if p.is_file())
    paths.update((ROOT / "artifacts/benchmarks").glob("step10-analysis-*.json"))
    paths.update(
        p for p in release.iterdir() if p.is_file() and p.suffix in {".json", ".md", ".png"}
    )
    for directory in (ROOT / "artifacts").glob("step10*"):
        if directory.is_dir():
            paths.update(
                p
                for p in directory.rglob("*")
                if p.is_file() and p.suffix in {".json", ".jsonl", ".md", ".png", ".log"}
            )
    paths.update(p for p in soak.iterdir() if p.is_file() and p.suffix in {".json", ".jsonl"})
    for historical in (ROOT / "artifacts/step10").glob("live-soak*"):
        if historical.is_dir():
            paths.update(
                p for p in historical.iterdir() if p.is_file() and p.suffix in {".json", ".jsonl"}
            )
    secrets = [p.read_bytes().strip() for p in (ROOT / ".secrets").iterdir() if p.is_file()]
    secrets = [v for v in secrets if v]
    hashes = {}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Validate every selected file before creating the archive; print file names, never values.
    for path in sorted(paths):
        assert ROOT in path.resolve().parents and not path.is_symlink()
        relative = path.relative_to(ROOT).as_posix()
        assert ".." not in Path(relative).parts and not relative.startswith("/"), relative
        assert ".secrets" not in path.parts and path.name != ".env"
        content = path.read_bytes()
        assert not any(value in content for value in secrets), (
            "secret value found in selected file: " + relative
        )
        hashes[relative] = hashlib.sha256(content).hexdigest()
    manifest = {
        "captured_at": datetime.now(UTC).isoformat(),
        "files": hashes,
        "completed_soak": soak.relative_to(ROOT).as_posix(),
        "scope": "Local source and seven formal frozen experiments, measured summaries, final v6 and historical step10 engineering/soak evidence; excludes keys, .env, dependency installations, DB dumps and unrelated projects. Earlier step3-9 engineering evidence remains in the original workspace.",
    }
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for relative, digest in hashes.items():
            content = (ROOT / relative).read_bytes()
            assert hashlib.sha256(content).hexdigest() == digest, "file changed while packaging"
            archive.writestr(relative, content)
        archive.writestr(
            "DELIVERY-MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        )
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        for relative, digest in hashes.items():
            assert hashlib.sha256(archive.read(relative)).hexdigest() == digest
        manifest_bytes = archive.read("DELIVERY-MANIFEST.json")
        assert json.loads(manifest_bytes)["files"] == hashes
        for name in archive.namelist():
            assert ".secrets" not in Path(name).parts and Path(name).name != ".env"
            assert not any(value in archive.read(name) for value in secrets), (
                "secret value found in archive member: " + name
            )
        with tempfile.TemporaryDirectory(prefix="l4-agent-delivery-verify-") as scratch:
            extracted = Path(scratch)
            archive.extractall(extracted)
            for relative, digest in hashes.items():
                unpacked = extracted / Path(*relative.split("/"))
                assert unpacked.is_file()
                assert hashlib.sha256(unpacked.read_bytes()).hexdigest() == digest
            extracted_manifest = json.loads(
                (extracted / "DELIVERY-MANIFEST.json").read_text(encoding="utf-8")
            )
            assert extracted_manifest["files"] == hashes
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "archive": str(output.relative_to(ROOT)),
        "files": len(hashes),
        "bytes": output.stat().st_size,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "secret_scan": "passed",
        "archive_readback_sha256": "passed",
        "unzip_extraction_and_sha256": "passed",
    }
    output.with_suffix(".manifest.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=ROOT / "artifacts/delivery/l4-agent-mvp-v6.zip"
    )
    parser.add_argument(
        "--soak-directory",
        type=Path,
        default=ROOT / "artifacts/step10/live-soak-final-v6",
    )
    main(parser.parse_args())
