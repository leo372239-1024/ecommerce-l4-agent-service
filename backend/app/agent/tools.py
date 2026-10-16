"""Versioned read-only commerce simulator. This is not a real merchant integration."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import text


class ToolError(RuntimeError):
    def __init__(self, code: str, retryable: bool = False, usage=None):
        super().__init__(code)
        self.code, self.retryable = code, retryable
        self.usage = usage or {}


def envelope(entity_id, version, fields, source="commerce_simulator"):
    now = datetime.now(UTC)
    return {
        "evidence_id": f"{source}:{entity_id}:v{version}",
        "source": source,
        "entity_id": entity_id,
        "entity_version": version,
        "observed_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=120)).isoformat(),
        "schema_version": 1,
        "trust": "authoritative_simulator",
        "fields": fields,
    }


class ReadGateway:
    def __init__(self, engine, customer_id):
        self.engine, self.customer_id = engine, customer_id

    async def order(self, order_id):
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, version, payload FROM commerce_orders WHERE id=:id AND customer_id=:owner"
                        ),
                        {"id": order_id, "owner": self.customer_id},
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            # Same response for nonexistent and someone else's order.
            raise ToolError("order_unavailable")
        return row

    async def execute(self, node, evidence):
        # Every tool independently rechecks ownership against the authoritative DB.
        row = await self.order(node.order_id)
        payload, version = row["payload"], row["version"]
        prior = evidence.get(f"{node.order_id}:get_order")
        if prior and prior["entity_version"] != version and node.tool != "get_order":
            raise ToolError("stale_environment", retryable=True)
        # Reserved seeded fault fixtures model upstream latency/errors, not caller-controlled hooks.
        delay = payload.get("simulator_delay_seconds", 0)
        if delay:
            await asyncio.sleep(delay)
            row = await self.order(node.order_id)
            payload, version = row["payload"], row["version"]
            if prior and prior["entity_version"] != version and node.tool != "get_order":
                raise ToolError("stale_environment", retryable=True)
        if payload.get("simulator_transient_error"):
            raise ToolError("upstream_unavailable", retryable=True)
        if node.tool == "get_order":
            fields = {
                key: payload[key] for key in ("status", "paid_minor", "currency", "delivered_days")
            }
        elif node.tool == "get_logistics":
            fields = {"status": payload["logistics"], "order_status": payload["status"]}
        elif node.tool == "get_policy":
            return envelope(
                "return-policy",
                1,
                {"max_delivered_days": 7, "currency": "CNY", "refund_basis": "actual_paid_minor"},
                "verified_policy",
            )
        elif node.tool == "check_eligibility":
            policy = evidence[f"{node.order_id}:get_policy"]["fields"]
            eligible = (
                payload["status"] == "DELIVERED"
                and payload["delivered_days"] <= policy["max_delivered_days"]
            )
            fields = {
                "eligible": eligible,
                "reason": "within_return_window" if eligible else "outside_policy",
            }
        elif node.tool == "quote_refund":
            eligibility = evidence[f"{node.order_id}:check_eligibility"]
            if not eligibility["fields"]["eligible"]:
                return envelope(
                    node.order_id,
                    version,
                    {"available": False, "currency": "CNY", "reason": "outside_policy"},
                )
            fields = {
                "available": True,
                "amount_minor": payload["paid_minor"],
                "currency": "CNY",
                "is_estimate": True,
                "requires_return_inspection": True,
            }
        else:
            raise ToolError("tool_not_allowed")
        return envelope(node.order_id, version, fields)


def verify(node, observation, evidence):
    if observation["schema_version"] != 1 or not observation["evidence_id"]:
        raise ToolError("invalid_evidence")
    if node.risk == "write":
        fields = observation["fields"]
        # An immutable committed receipt remains valid after observation-cache expiry.
        if (
            observation["entity_id"] != node.order_id
            or fields.get("status") != "SUCCEEDED"
            or fields.get("action") != node.tool
            or not fields.get("operation_id")
            or type(fields.get("amount_minor")) is not int
            or fields["amount_minor"] < 0
            or fields.get("currency") != "CNY"
        ):
            raise ToolError("invalid_write_receipt")
        return True
    if datetime.fromisoformat(observation["expires_at"]) < datetime.now(UTC):
        raise ToolError("expired_evidence", retryable=True)
    fields = observation["fields"]
    if node.tool == "quote_refund":
        if fields.get("available") is False:
            if evidence[f"{node.order_id}:check_eligibility"]["fields"]["eligible"]:
                raise ToolError("quote_fact_conflict")
            return True
        amount = fields.get("amount_minor")
        if type(amount) is not int or amount < 0 or fields.get("currency") != "CNY":
            raise ToolError("invalid_money")
        order_fields = evidence[f"{node.order_id}:get_order"]["fields"]
        if amount != order_fields.get("remaining_minor", order_fields["paid_minor"]):
            raise ToolError("quote_fact_conflict")
    return True
