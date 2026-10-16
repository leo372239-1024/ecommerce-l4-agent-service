"""One migration job initializes SDK schema, then seeds isolated DEVELOPMENT accounts."""

import asyncio
import json
import subprocess

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.conninfo import make_conninfo
from sqlalchemy import text

from app.business.fixtures import details
from app.core.auth import hash_password
from app.core.config import get_settings
from app.core.resources import Resources


def conninfo(settings):
    return make_conninfo(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        dbname=settings.postgres_db,
        password=settings.postgres_password.get_secret_value(),
        connect_timeout=5,
    )


async def initialize():
    settings = get_settings()
    async with AsyncPostgresSaver.from_conn_string(conninfo(settings)) as saver:
        await saver.setup()
    if settings.app_env == "production":
        return  # Operators provision accounts and OIDC bindings; no production demo fixtures.
    resources = Resources(settings)
    try:
        async with resources.engine.begin() as connection:
            for name, owner, password, role in (
                (
                    "alice",
                    "customer-alice",
                    settings.demo_alice_password.get_secret_value(),
                    "customer",
                ),
                ("bob", "customer-bob", settings.demo_bob_password.get_secret_value(), "customer"),
                (
                    "supervisor",
                    "staff-supervisor",
                    settings.demo_supervisor_password.get_secret_value(),
                    "supervisor",
                ),
                (
                    "support",
                    "staff-support",
                    settings.demo_support_password.get_secret_value(),
                    "support",
                ),
            ):
                if not password:
                    raise RuntimeError("demo password file missing; run bootstrap first")
                await connection.execute(
                    text(
                        "INSERT INTO customer_accounts (id,username,password_hash,role) VALUES (:id,:name,:hash,:role) ON CONFLICT (id) DO NOTHING"
                    ),
                    {
                        "id": owner,
                        "name": name,
                        "role": role,
                        "hash": await asyncio.to_thread(hash_password, password),
                    },
                )
            fixtures = [
                ("ORD-1001", "customer-alice", "DELIVERED", 12900, 3, "已签收"),
                ("ORD-1002", "customer-alice", "SHIPPED", 8900, 0, "运输中"),
                ("ORD-1003", "customer-alice", "DELIVERED", 5900, 12, "已签收"),
                ("ORD-2001", "customer-bob", "DELIVERED", 21900, 2, "已签收"),
                ("ORD-1004", "customer-alice", "DELIVERED", 4900, 2, "已签收"),
                ("ORD-1005", "customer-alice", "PAID", 3900, 0, "未发货"),
            ]
            for order, owner, status, paid, days, logistics in fixtures:
                await connection.execute(
                    text(
                        "INSERT INTO commerce_orders VALUES (:id,:owner,1,CAST(:payload AS jsonb)) "
                        "ON CONFLICT (id) DO NOTHING"
                    ),
                    {
                        "id": order,
                        "owner": owner,
                        "payload": json.dumps(
                            {
                                "status": status,
                                "paid_minor": paid,
                                "currency": "CNY",
                                "delivered_days": days,
                                "logistics": logistics,
                            }
                        ),
                    },
                )
            # Populate missing simulator facts without resetting any existing business effects.
            rows = (
                (await connection.execute(text("SELECT id,payload FROM commerce_orders")))
                .mappings()
                .all()
            )
            for row in rows:
                if "details" not in row["payload"]:
                    value = {"details": details(row["id"], row["payload"]["paid_minor"])}
                    if row["id"] == "ORD-1004":
                        value["return_state"] = "ACCEPTED"
                    await connection.execute(
                        text(
                            "UPDATE commerce_orders SET payload=payload || CAST(:details AS jsonb) WHERE id=:id"
                        ),
                        {"id": row["id"], "details": json.dumps(value, ensure_ascii=False)},
                    )
    finally:
        await resources.close()


if __name__ == "__main__":
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    asyncio.run(initialize())
