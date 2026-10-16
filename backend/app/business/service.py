"""Authoritative simulator: all money/state/receipt/outbox changes share one transaction."""

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from app.agent.tools import ToolError, envelope
from app.business.catalog import CATALOG, ReadArgs, ReceiptArgs, WriteArgs, canonical_hash


async def one(conn, sql, params):
    return (await conn.execute(text(sql), params)).mappings().first()


def allowed_action(action, payload, policy):
    remaining = payload["paid_minor"] - payload.get("refunded_minor", 0)
    within = (
        payload["status"] == "DELIVERED" and payload["delivered_days"] <= policy["max_return_days"]
    )
    if action == "create_return":
        return within and payload.get("return_state", "NONE") == "NONE" and remaining > 0
    if action == "submit_refund":
        return within and payload.get("return_state") == "ACCEPTED" and remaining > 0
    if action == "cancel_order":
        return payload["status"] == "PAID" and remaining > 0
    return False


def validate_business_data(payload):
    paid, refunded = payload.get("paid_minor"), payload.get("refunded_minor", 0)
    days = payload.get("delivered_days")
    if (
        type(paid) is not int
        or type(refunded) is not int
        or not 0 <= refunded <= paid
        or type(days) is not int
        or days < 0
        or payload.get("currency") != "CNY"
        or payload.get("status") not in {"PAID", "SHIPPED", "DELIVERED", "CANCELLED"}
    ):
        raise ToolError("invalid_business_data")


def projection(tool, p, policy):
    # Snapshot data is explicitly seeded business data, never inferred by an LLM.
    paid, refunded = p["paid_minor"], p.get("refunded_minor", 0)
    remaining = paid - refunded
    eligible = (
        p["status"] == "DELIVERED"
        and p["delivered_days"] <= policy["max_return_days"]
        and remaining > 0
    )
    fields = {
        "get_order": {
            **{k: p[k] for k in ("status", "paid_minor", "currency", "delivered_days")},
            "remaining_minor": remaining,
        },
        "get_logistics": {"status": p["logistics"], "order_status": p["status"]},
        "get_policy": {
            "max_delivered_days": policy["max_return_days"],
            "currency": "CNY",
            "refund_basis": "remaining_actual_paid_minor",
            "supervisor_threshold_minor": policy["supervisor_threshold_minor"],
        },
        "check_eligibility": {
            "eligible": eligible,
            "reason": "within_return_window" if eligible else "outside_policy",
        },
        "quote_refund": {
            "available": eligible and remaining > 0,
            "amount_minor": remaining,
            "currency": "CNY",
            "is_estimate": True,
            "requires_return_inspection": p.get("return_state") != "ACCEPTED",
        },
        "get_refund_balance": {
            "paid_minor": paid,
            "refunded_minor": refunded,
            "remaining_minor": remaining,
            "currency": "CNY",
        },
        "get_return_status": {
            "status": p.get("return_state", "NONE"),
            "return_id": p.get("return_id"),
        },
        "get_refund_status": {
            "status": "REFUNDED" if refunded else "NOT_REFUNDED",
            "refunded_minor": refunded,
            "transaction_id": p.get("refund_transaction_id"),
        },
        "get_cancellation_eligibility": {
            "eligible": allowed_action("cancel_order", p, policy),
            "order_status": p["status"],
        },
        "get_address_change_eligibility": {
            "eligible": p["status"] == "PAID",
            "order_status": p["status"],
        },
        "get_warranty_eligibility": {
            "eligible": p["delivered_days"] <= 365,
            "elapsed_days": p["delivered_days"],
            "warranty_days": 365,
        },
        "get_loyalty_impact": {
            "points_to_reverse": remaining // 100,
            "basis": "remaining_paid_yuan",
        },
    }
    if tool in fields:
        return fields[tool]
    # Unlike missing upstream data, a verified empty list is a legitimate business fact.
    data = p.get("details", {})
    if tool not in data:
        raise ToolError("business_data_unavailable")
    return data[tool]


class MerchantService:
    def __init__(self, engine):
        self.engine = engine

    async def context(self, conn, workflow_id, fencing, order_id, *, write=False):
        # Fixed lock order used by execution and approval: run, account, policy, order, proposal.
        suffix = " FOR UPDATE" if write else ""
        run = await one(
            conn, "SELECT * FROM agent_runs WHERE id=:id" + suffix, {"id": str(workflow_id)}
        )
        if (
            not run
            or run["fencing"] != fencing
            or run["status"] != "RUNNING"
            or not run["lease_until"]
            or run["lease_until"] <= datetime.now(UTC)
        ):
            raise ToolError("stale_execution_authority")
        if write and run["cancel_requested"]:
            raise ToolError("run_cancelled")
        actor = await one(
            conn,
            "SELECT * FROM customer_accounts WHERE id=:id" + (" FOR SHARE" if write else ""),
            {"id": run["customer_id"]},
        )
        if not actor or not actor["active"] or actor["role"] != "customer":
            raise ToolError("permission_revoked")
        policy = await one(
            conn,
            "SELECT * FROM merchant_policies WHERE tenant_id=:tenant"
            + (" FOR SHARE" if write else ""),
            {"tenant": actor["tenant_id"]},
        )
        order = await one(
            conn,
            "SELECT o.* FROM commerce_orders o JOIN commerce_order_tenants t ON t.order_id=o.id WHERE o.id=:id AND o.customer_id=:owner AND t.tenant_id=:tenant"
            + (" FOR UPDATE OF o FOR SHARE OF t" if write else ""),
            {"id": order_id, "owner": actor["id"], "tenant": actor["tenant_id"]},
        )
        if not order or not policy:
            raise ToolError("order_unavailable")
        validate_business_data(order["payload"])
        return run, actor, policy, order

    async def read(self, tool, args, workflow_id, fencing):
        model = (ReceiptArgs if tool == "get_operation_receipt" else ReadArgs).model_validate(args)
        if tool not in CATALOG or CATALOG[tool].risk != "read":
            raise ToolError("tool_not_allowed")
        async with self.engine.begin() as conn:
            _, _, policy, order = await self.context(conn, workflow_id, fencing, model.order_id)
            p = order["payload"]
            if p.get("simulator_transient_error"):
                raise ToolError("upstream_unavailable", True)
            if p.get("simulator_delay_seconds"):
                import asyncio

                await asyncio.sleep(p["simulator_delay_seconds"])
                _, _, policy, order = await self.context(conn, workflow_id, fencing, model.order_id)
                p = order["payload"]
            if tool == "get_ticket_status":
                tickets = (
                    (
                        await conn.execute(
                            text(
                                "SELECT id,status,revision FROM handoff_cases WHERE customer_id=:owner AND :order=ANY(order_ids) ORDER BY created_at DESC LIMIT 100"
                            ),
                            {"owner": order["customer_id"], "order": model.order_id},
                        )
                    )
                    .mappings()
                    .all()
                )
                fields = {
                    "tickets": [
                        {
                            "case_id": str(ticket["id"]),
                            "status": ticket["status"],
                            "revision": ticket["revision"],
                        }
                        for ticket in tickets
                    ]
                }
            elif tool == "get_operation_receipt":
                operation_filter = " AND b.operation_id=:operation" if model.operation_id else ""
                rows = (
                    (
                        await conn.execute(
                            text(
                                "SELECT b.operation_id,b.status,b.receipt FROM business_operations b JOIN business_proposals p USING(operation_id) WHERE p.order_id=:id AND p.customer_id=:owner"
                                + operation_filter
                                + " ORDER BY p.created_at DESC LIMIT 101"
                            ),
                            {
                                "id": model.order_id,
                                "owner": order["customer_id"],
                                "operation": model.operation_id,
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
                fields = {
                    "operations": [
                        dict(row) | {"operation_id": str(row["operation_id"])} for row in rows[:100]
                    ],
                    "has_more": len(rows) > 100,
                }
            else:
                fields = projection(tool, p, policy)
            if tool == "get_policy":
                result = envelope(
                    "return-policy:" + policy["tenant_id"],
                    policy["version"],
                    fields,
                    "verified_policy",
                )
            else:
                result = envelope(model.order_id, order["version"], fields)
            result["evidence_id"] += ":" + tool
            result["dependency_versions"] = {
                "return-policy:" + policy["tenant_id"]: policy["version"]
            }
            return result

    async def prepare(
        self, action, order_id, workflow_id, fencing, expected_version, expected_policy_version
    ):
        async with self.engine.begin() as conn:
            _, actor, policy, order = await self.context(
                conn, workflow_id, fencing, order_id, write=True
            )
            if order["version"] != expected_version or policy["version"] != expected_policy_version:
                raise ToolError("stale_environment")
            if not allowed_action(action, order["payload"], policy):
                raise ToolError("business_precondition_failed")
            amount = (
                0
                if action == "create_return"
                else order["payload"]["paid_minor"] - order["payload"].get("refunded_minor", 0)
            )
            value = {
                "action": action,
                "order_id": order_id,
                "entity_version": order["version"],
                "policy_version": policy["version"],
                "amount_minor": amount,
                "currency": "CNY",
                "customer_id": actor["id"],
                "tenant_id": actor["tenant_id"],
            }
            arg_hash, operation = canonical_hash(value), str(uuid.uuid4())
            await conn.execute(
                text(
                    "INSERT INTO business_proposals (operation_id,workflow_id,customer_id,tenant_id,order_id,action,entity_version,policy_version,amount_minor,arg_hash,requires_supervisor) VALUES (:operation,:workflow,:owner,:tenant,:order,:action,:version,:policy,:amount,:hash,:supervisor) ON CONFLICT(workflow_id,order_id,action,entity_version,policy_version) DO NOTHING"
                ),
                {
                    "operation": operation,
                    "workflow": str(workflow_id),
                    "owner": actor["id"],
                    "tenant": actor["tenant_id"],
                    "order": order_id,
                    "action": action,
                    "version": order["version"],
                    "policy": policy["version"],
                    "amount": amount,
                    "hash": arg_hash,
                    "supervisor": amount > policy["supervisor_threshold_minor"],
                },
            )
            row = await one(
                conn,
                "SELECT * FROM business_proposals WHERE workflow_id=:id AND order_id=:order AND action=:action AND entity_version=:version AND policy_version=:policy",
                {
                    "id": str(workflow_id),
                    "order": order_id,
                    "action": action,
                    "version": order["version"],
                    "policy": policy["version"],
                },
            )
            return dict(row)

    async def write(self, tool, args, workflow_id, fencing):
        model = WriteArgs.model_validate(args)
        if tool not in CATALOG or CATALOG[tool].risk != "write":
            raise ToolError("tool_not_allowed")
        async with self.engine.begin() as conn:
            _, actor, policy, order = await self.context(
                conn, workflow_id, fencing, model.order_id, write=True
            )
            proposal = await one(
                conn,
                "SELECT * FROM business_proposals WHERE operation_id=:id FOR UPDATE",
                {"id": model.operation_id},
            )
            if (
                not proposal
                or proposal["workflow_id"] != uuid.UUID(str(workflow_id))
                or proposal["customer_id"] != actor["id"]
                or proposal["tenant_id"] != actor["tenant_id"]
                or proposal["order_id"] != model.order_id
                or proposal["action"] != tool
                or proposal["arg_hash"] != model.arg_hash
            ):
                raise ToolError("operation_contract_mismatch")
            existing = await one(
                conn,
                "SELECT * FROM business_operations WHERE operation_id=:id FOR UPDATE",
                {"id": model.operation_id},
            )
            if existing and existing["arg_hash"] != model.arg_hash:
                raise ToolError("operation_argument_conflict")
            if existing and existing["status"] == "SUCCEEDED":
                return existing[
                    "receipt"
                ]  # Recorded effects survive expired approval and version changes.
            if (
                proposal["status"] != "APPROVED"
                or proposal["expires_at"] <= datetime.now(UTC)
                or proposal["entity_version"] != order["version"]
                or proposal["policy_version"] != policy["version"]
            ):
                raise ToolError("approval_invalid_or_stale")
            if proposal["confirmed_by"] != actor["id"]:
                raise ToolError("customer_confirmation_required")
            if proposal["requires_supervisor"]:
                approver = await one(
                    conn,
                    "SELECT * FROM customer_accounts WHERE id=:id FOR SHARE",
                    {"id": proposal["approved_by"]},
                )
                if (
                    not approver
                    or not approver["active"]
                    or approver["role"] != "supervisor"
                    or approver["tenant_id"] != actor["tenant_id"]
                ):
                    raise ToolError("supervisor_authority_revoked")
            p = dict(order["payload"])
            if not allowed_action(tool, p, policy):
                raise ToolError("business_precondition_failed")
            remaining = p["paid_minor"] - p.get("refunded_minor", 0)
            amount = 0 if tool == "create_return" else remaining
            if (
                amount != proposal["amount_minor"]
                or p["currency"] != "CNY"
                or type(amount) is not int
                or amount < 0
            ):
                raise ToolError("money_contract_mismatch")
            if tool == "create_return":
                p.update(return_state="REQUESTED", return_id="RET-" + model.operation_id)
            else:
                p.update(
                    refunded_minor=p.get("refunded_minor", 0) + amount,
                    refund_transaction_id="SIM-REF-" + model.operation_id,
                )
                if tool == "cancel_order":
                    p.update(status="CANCELLED", logistics="订单已取消")
                else:
                    p.update(return_state="REFUNDED")
            version = order["version"] + 1
            receipt = envelope(
                model.order_id,
                version,
                {
                    "operation_id": model.operation_id,
                    "action": tool,
                    "status": "SUCCEEDED",
                    "amount_minor": amount,
                    "currency": "CNY",
                    "order_status": p["status"],
                    "return_state": p.get("return_state", "NONE"),
                    "transaction_id": p.get("refund_transaction_id"),
                    "simulated_business": True,
                },
            )
            await conn.execute(
                text(
                    "UPDATE commerce_orders SET version=:version,payload=CAST(:payload AS jsonb) WHERE id=:id AND version=:old"
                ),
                {
                    "version": version,
                    "payload": json.dumps(p),
                    "id": model.order_id,
                    "old": order["version"],
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO business_operations(operation_id,arg_hash,status,receipt,completed_at) VALUES(:id,:hash,'SUCCEEDED',CAST(:receipt AS jsonb),now()) ON CONFLICT(operation_id) DO UPDATE SET status='SUCCEEDED',receipt=EXCLUDED.receipt,completed_at=now()"
                ),
                {"id": model.operation_id, "hash": model.arg_hash, "receipt": json.dumps(receipt)},
            )
            await conn.execute(
                text(
                    "UPDATE business_proposals SET status='EXECUTED',revision=revision+1 WHERE operation_id=:id"
                ),
                {"id": model.operation_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO merchant_outbox(event_id,order_id,entity_version,payload) VALUES(:id,:order,:version,CAST(:payload AS jsonb))"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "order": model.order_id,
                    "version": version,
                    "payload": json.dumps(
                        {"operation_id": model.operation_id, "action": tool, "version": version}
                    ),
                },
            )
        return receipt

    async def consume_events(self):
        # PG outbox is durable transport here; Redis notifications are optional latency hints.
        async with self.engine.begin() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM merchant_outbox WHERE delivered_at IS NULL ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 100"
                        )
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                inserted = await conn.scalar(
                    text(
                        "INSERT INTO merchant_inbox(event_id) VALUES(:id) ON CONFLICT DO NOTHING RETURNING event_id"
                    ),
                    {"id": row["event_id"]},
                )
                if inserted:
                    await conn.execute(
                        text(
                            "INSERT INTO environment_versions VALUES(:id,:version) ON CONFLICT(order_id) DO UPDATE SET entity_version=greatest(environment_versions.entity_version,EXCLUDED.entity_version)"
                        ),
                        {"id": row["order_id"], "version": row["entity_version"]},
                    )
                await conn.execute(
                    text("UPDATE merchant_outbox SET delivered_at=now() WHERE event_id=:id"),
                    {"id": row["event_id"]},
                )
