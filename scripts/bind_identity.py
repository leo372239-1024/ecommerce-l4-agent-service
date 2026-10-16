"""Operator-only local provisioning; no public identity binding endpoint."""

import argparse
import asyncio
import sys
from getpass import getpass
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.auth import digest
from app.core.config import get_settings
from app.core.resources import Resources


async def bind(args, subject):
    resources = Resources(get_settings())
    try:
        async with resources.engine.begin() as conn:
            account = await conn.scalar(
                text("SELECT id FROM customer_accounts WHERE id=:id AND active"),
                {"id": args.account},
            )
            if not account:
                raise ValueError("active_account_required")
            await conn.execute(
                text(
                    "INSERT INTO identity_bindings(issuer,subject_hash,customer_id) VALUES(:issuer,:sub,:id)"
                ),
                {"issuer": args.issuer, "sub": digest(subject), "id": account},
            )
        print(
            "Identity bound to the existing local account; role and tenant remain authoritative in PostgreSQL."
        )
    finally:
        await resources.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--account", required=True)
    args = parser.parse_args()
    if urlsplit(args.issuer).scheme != "https":
        parser.error("HTTPS issuer required")
    subject = getpass("OIDC subject (hidden): ")
    if not 1 <= len(subject) <= 255:
        parser.error("Subject must contain 1-255 characters")
    asyncio.run(bind(args, subject))


if __name__ == "__main__":
    main()
