"""Create local-only settings/secrets; never overwrite existing credentials."""

import os
import re
import secrets
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    secret_dir = ROOT / ".secrets"
    secret_dir.mkdir(exist_ok=True)
    if os.name == "nt":
        identity = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, check=True
        )
        match = re.search(rb"\bS-1-\d+(?:-\d+)+\b", identity.stdout)
        if not match:
            raise RuntimeError("Could not determine the current Windows account SID")
        sid = match.group().decode("ascii")
        subprocess.run(
            [
                "icacls",
                str(secret_dir),
                "/inheritance:r",
                "/grant:r",
                f"*{sid}:(OI)(CI)F",
                "*S-1-5-18:(OI)(CI)F",
            ],
            capture_output=True,
            check=True,
        )
    else:
        secret_dir.chmod(0o700)
    for name in (
        "postgres_password",
        "grafana_admin_password",
        "demo_alice_password",
        "demo_bob_password",
        "demo_supervisor_password",
        "demo_support_password",
        "mcp_service_key",
    ):
        target = secret_dir / name
        if not target.exists():
            target.write_text(secrets.token_hex(24), encoding="utf-8")
        # File-based Compose secrets do not remap uid/gid. The private parent
        # protects the host; selected read-only mounts allow the nonroot service UID.
        target.chmod(0o644)
    for name in ("dashscope_api_key", "zhipu_api_key", "openrouter_api_key"):
        target = secret_dir / name
        if not target.exists():
            target.write_text("", encoding="utf-8")
        target.chmod(0o644)
    env = ROOT / ".env"
    if not env.exists():
        env.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    print("Local configuration prepared; existing settings and secrets preserved.")


if __name__ == "__main__":
    main()
