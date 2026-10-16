import json

from sqlalchemy import text

from app.agent.tools import ToolError
from app.business.mcp_client import MerchantClient
from app.business.routing import Router
from app.business.service import MerchantService, one


class Gateway:
    def __init__(self, settings, store, run):
        self.settings, self.engine, self.run = settings, store.engine, run
        self.client = MerchantClient(settings, run)
        self.router = Router(settings, store.engine, run)
        self.routing_info = {}

    async def execute(self, node, evidence, query=""):
        self.routing_info = {}
        from app.business.intents import exact_effect

        effect = exact_effect(query) if node.tool == "business_query" else node.tool
        try:
            tool, args, info = await self.router.select(query or node.tool, node.order_id, effect)
        except ToolError as exc:
            self.routing_info = {"node_id": node.id, **exc.usage}
            raise
        self.routing_info = {"node_id": node.id, "selected_tool": tool, **info}
        value = await self.client.invoke(tool, args)
        prior = evidence.get(f"{node.order_id}:get_order")
        policy = evidence.get(f"{node.order_id}:get_policy")
        if (
            policy
            and node.tool in {"check_eligibility", "quote_refund"}
            and value.get("dependency_versions", {}).get(policy["entity_id"])
            != policy["entity_version"]
        ):
            raise ToolError("stale_environment", True)
        if (
            prior
            and tool not in {"get_order", "get_policy"}
            and value["entity_version"] != prior["entity_version"]
        ):
            raise ToolError("stale_environment", True)
        return value

    async def prepare(self, node, evidence):
        return await MerchantService(self.engine).prepare(
            node.tool,
            node.order_id,
            self.run["id"],
            self.run["fencing"],
            evidence[f"{node.order_id}:get_order"]["entity_version"],
            evidence[f"{node.order_id}:get_policy"]["entity_version"],
        )

    async def proposal(self, operation_id):
        async with self.engine.connect() as conn:
            return await one(
                conn,
                "SELECT * FROM business_proposals WHERE operation_id=:id AND workflow_id=:workflow AND customer_id=:owner",
                {
                    "id": operation_id,
                    "workflow": str(self.run["id"]),
                    "owner": self.run["customer_id"],
                },
            )

    @staticmethod
    def validate_receipt(receipt, node, proposal):
        fields = receipt.get("fields", {})
        if (
            fields.get("operation_id") != str(proposal["operation_id"])
            or type(fields.get("amount_minor")) is not int
            or fields["amount_minor"] != proposal["amount_minor"]
            or fields.get("action") != node.tool
            or fields.get("status") != "SUCCEEDED"
            or fields.get("currency") != "CNY"
            or receipt.get("entity_id") != node.order_id
            or receipt.get("entity_version") != proposal["entity_version"] + 1
        ):
            raise ToolError("write_receipt_mismatch")

    async def commit(self, node, proposal):
        # Durable intent, independent of tool-call attempt/invocation/checkpoint lineage.
        params = {"id": str(proposal["operation_id"]), "hash": proposal["arg_hash"]}
        async with self.engine.begin() as conn:
            existing = await one(
                conn, "SELECT * FROM business_operations WHERE operation_id=:id", params
            )
            if existing and existing["arg_hash"] != params["hash"]:
                raise ToolError("operation_argument_conflict")
            if existing and existing["status"] == "REJECTED":
                raise ToolError("operation_already_rejected")
            await conn.execute(
                text(
                    "INSERT INTO business_operations(operation_id,arg_hash,status) VALUES(:id,:hash,'PREPARED') ON CONFLICT DO NOTHING"
                ),
                params,
            )
        # Query authoritative receipt before each possible dispatch, including crash recovery.
        receipt_args = {"order_id": node.order_id, "operation_id": params["id"]}
        observed = await self.client.invoke("get_operation_receipt", receipt_args)
        receipt = next(
            (
                item["receipt"]
                for item in observed["fields"]["operations"]
                if item["operation_id"] == params["id"] and item["status"] == "SUCCEEDED"
            ),
            None,
        )
        if receipt:
            self.validate_receipt(receipt, node, proposal)
            return receipt, {"reconciled": True, "dispatched": False}
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE business_operations SET status='DISPATCHED' WHERE operation_id=:id AND status IN ('PREPARED','RECONCILING','DISPATCHED')"
                ),
                params,
            )
        try:
            receipt = await self.client.invoke(
                node.tool,
                {
                    "order_id": node.order_id,
                    "operation_id": params["id"],
                    "arg_hash": params["hash"],
                },
            )
        except BaseException as exc:
            # Cancellation/kill is left durable for recovery, never converted into a false rejection.
            import asyncio

            if isinstance(exc, asyncio.CancelledError):
                raise
            async with self.engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE business_operations SET status='RECONCILING' WHERE operation_id=:id AND status='DISPATCHED'"
                    ),
                    params,
                )
            try:
                observed = await self.client.invoke("get_operation_receipt", receipt_args)
            except ToolError:
                raise ToolError("write_outcome_unknown") from None
            receipt = next(
                (
                    item["receipt"]
                    for item in observed["fields"]["operations"]
                    if item["operation_id"] == params["id"] and item["status"] == "SUCCEEDED"
                ),
                None,
            )
            if not receipt:
                if isinstance(exc, ToolError) and exc.code in {
                    "approval_invalid_or_stale",
                    "supervisor_authority_revoked",
                    "business_precondition_failed",
                    "operation_contract_mismatch",
                    "operation_argument_conflict",
                    "money_contract_mismatch",
                    "run_cancelled",
                    "permission_revoked",
                    "stale_execution_authority",
                }:
                    async with self.engine.begin() as conn:
                        await conn.execute(
                            text(
                                "UPDATE business_operations SET status='REJECTED',receipt=CAST(:failure AS jsonb),completed_at=now() WHERE operation_id=:id AND status='RECONCILING'"
                            ),
                            {**params, "failure": json.dumps({"error": exc.code})},
                        )
                    raise
                raise ToolError("write_outcome_unknown") from None
            self.validate_receipt(receipt, node, proposal)
            return receipt, {"reconciled": True, "dispatched": True}
        self.validate_receipt(receipt, node, proposal)
        return receipt, {"reconciled": False, "dispatched": True}
