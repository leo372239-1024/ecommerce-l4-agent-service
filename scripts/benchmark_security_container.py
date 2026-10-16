"""200 real MCP boundary probes; five correlated attack families, no LLM robustness claim."""

import asyncio
import json
import time
import uuid
from pathlib import Path

from sqlalchemy import text

from app.agent.store import RunStore
from app.agent.tools import ToolError
from app.business.catalog import CATALOG
from app.business.mcp_client import MerchantClient
from app.core.config import get_settings
from app.core.resources import Resources


async def main():
    settings = get_settings()
    if settings.postgres_db != "agent_benchmark_step9":
        raise RuntimeError("isolated_database_required")
    resources = Resources(settings)
    output = Path("/artifacts/benchmarks/step9-security")
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    results = []
    try:
        store = RunStore(resources.engine)
        run = await store.create(
            "customer-alice", "MCP边界安全测试", uuid.uuid4().hex, uuid.uuid4().hex, "evaluation"
        )
        row = await store.claim("boundary-probe", 120, run["id"])
        before = None
        async with resources.engine.connect() as conn:
            before = (
                await conn.execute(
                    text(
                        "SELECT id,version,payload FROM commerce_orders WHERE id IN ('ORD-1004','ORD-2001') ORDER BY id"
                    )
                )
            ).all()
        for tool, spec in CATALOG.items():
            for family in (
                "null_entity",
                "wrong_entity_type",
                "foreign_entity",
                "identity_override",
                "obsolete_fence",
            ):
                args = {"order_id": "ORD-1004"}
                if spec.risk == "write":
                    args.update(operation_id=str(uuid.uuid4()), arg_hash="0" * 64)
                fence = row["fencing"]
                if family == "null_entity":
                    args["order_id"] = None
                elif family == "wrong_entity_type":
                    args["order_id"] = 1004
                elif family == "foreign_entity":
                    args["order_id"] = "ORD-2001"
                elif family == "identity_override":
                    args["tenant_id"] = "attacker-tenant"
                else:
                    fence += 1
                client = MerchantClient(settings, {"id": row["id"], "fencing": fence})
                try:
                    await client.invoke(tool, args)
                    results.append({"tool": tool, "family": family, "attack_succeeded": True})
                except ToolError as exc:
                    results.append(
                        {
                            "tool": tool,
                            "family": family,
                            "attack_succeeded": False,
                            "code": exc.code,
                        }
                    )
                if len(results) % 20 == 0:
                    await store.check(row["id"], row["fencing"], 120, 600)
        async with resources.engine.connect() as conn:
            after = (
                await conn.execute(
                    text(
                        "SELECT id,version,payload FROM commerce_orders WHERE id IN ('ORD-1004','ORD-2001') ORDER BY id"
                    )
                )
            ).all()
            operations = await conn.scalar(
                text(
                    "SELECT count(*) FROM business_operations b JOIN business_proposals p USING(operation_id) WHERE p.workflow_id=:id"
                ),
                {"id": str(row["id"])},
            )
        report = {
            "probes": len(results),
            "attack_families": 5,
            "tools": len(CATALOG),
            "elapsed_seconds": time.monotonic() - started,
            "observed_successful_attacks": sum(r["attack_succeeded"] for r in results),
            "unchanged_order_snapshots": before == after,
            "operations": operations,
            "model_involvement": "none; shared gateway/MCP enforcement only",
            "independence": "Correlated tool-by-attack matrix. Do not interpret 200 probes as 200 independent scenarios or general prompt-injection robustness.",
            "results": results,
        }
        async with resources.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE agent_runs SET status='CANCELLED',cancel_requested=true,lease_until=NULL WHERE id=:id"
                ),
                {"id": str(row["id"])},
            )
        (output / "boundary-probes.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k != "results"}))
        assert (
            len(results) == 200
            and not report["observed_successful_attacks"]
            and before == after
            and not operations
        )
    finally:
        await resources.close()


if __name__ == "__main__":
    asyncio.run(main())
