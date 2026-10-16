"""Check eligible secrets, document links, deployed backend sources and served web assets."""

import argparse
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/step5")
    args = parser.parse_args()
    paths = (
        subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        )
        .stdout.decode("utf-8")
        .split("\0")
    )
    paths = [p for p in paths if p and (ROOT / p).is_file()]
    secrets = [p.read_bytes().strip() for p in (ROOT / ".secrets").iterdir() if p.is_file()]
    secrets = [v for v in secrets if v]
    assert secrets, "no local secrets to compare"
    leaks, broken, fingerprints = [], [], {}
    for name in paths:
        path = ROOT / name
        content = path.read_bytes()
        if any(value in content for value in secrets):
            leaks.append(name)
        fingerprints[name] = hashlib.sha256(content).hexdigest()
        if path.suffix == ".md":
            for link in re.findall(r"\]\(([^)]+)\)", content.decode("utf-8")):
                target = link.strip("<>").split("#", 1)[0]
                if not target or urlsplit(target).scheme or target.startswith("/"):
                    continue
                if not (path.parent / unquote(target)).exists():
                    broken.append({"file": name, "target": target})
    assert not leaks, f"secret value found in eligible files: {leaks}"
    assert not broken, f"broken document links: {broken}"
    source = """
import hashlib,json
from pathlib import Path
names=[p for d in ('app','migrations') for p in Path(d).rglob('*.py')]+[Path('alembic.ini')]
print(json.dumps({p.as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in names}))
"""
    deployed = {}
    for service in ("api", "worker", "merchant-mcp"):
        result = subprocess.run(
            ["docker", "exec", "-i", f"l4-agent-{service}-1", "python", "-"],
            input=source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        hashes = json.loads(result.stdout)
        assert hashes and all(
            fingerprints["backend/" + name] == value for name, value in hashes.items()
        )
        image_id = subprocess.run(
            ["docker", "inspect", "--format", "{{.Image}}", f"l4-agent-{service}-1"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        deployed[service] = {"image_id": image_id, "source_files_verified": len(hashes)}
    web_container = "l4-agent-web-1"
    web_image = subprocess.run(
        ["docker", "inspect", "--format", "{{.Config.Image}}", web_container],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    web_image_id = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", web_image],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    running_image_id = subprocess.run(
        ["docker", "inspect", "--format", "{{.Image}}", web_container],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert web_image_id == running_image_id, "web is not running the latest locally built image"

    def web_file(path):
        return subprocess.run(
            ["docker", "exec", web_container, "cat", "/usr/share/nginx/html" + path],
            capture_output=True,
            check=True,
        ).stdout

    assets = re.findall(rb'(?:src|href)="(/assets/[^\"]+)"', web_file("/index.html"))
    assert assets, "web build has no linked assets"
    web_hashes = {}
    for asset in [b"/index.html", *assets]:
        path = asset.decode("utf-8")
        with build_opener(ProxyHandler({})).open(
            "http://localhost:8080" + path, timeout=15
        ) as response:
            content = response.read()
            assert response.headers.get("Content-Security-Policy"), path
        assert content == web_file(path), f"served web asset differs from image: {path}"
        web_hashes[path] = hashlib.sha256(content).hexdigest()
    deployed["web"] = {
        "image_id": running_image_id,
        "served_assets_verified": len(web_hashes),
        "sha256": web_hashes,
    }
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "secret_values_checked": len(secrets),
        "eligible_files": len(paths),
        "secret_scan": "passed",
        "local_document_links": "passed",
        "deployed": deployed,
        "sha256": fingerprints,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "source-manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in report.items() if k != "sha256"}))


if __name__ == "__main__":
    main()
