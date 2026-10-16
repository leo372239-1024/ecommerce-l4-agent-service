import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from sqlalchemy import text

from app.agent.contracts import StrictModel
from app.business.service import one
from app.core.auth import Principal, principal

router = APIRouter()
Actor = Annotated[Principal, Depends(principal)]


class Decision(StrictModel):
    expected_revision: int = Field(ge=1)
    decision: Literal["confirm", "approve", "reject"]


def visible(row):
    return {
        key: row[key]
        for key in (
            "operation_id",
            "workflow_id",
            "order_id",
            "action",
            "entity_version",
            "policy_version",
            "amount_minor",
            "arg_hash",
            "status",
            "revision",
            "requires_supervisor",
            "expires_at",
        )
    }


@router.get("/approvals")
async def list_approvals(request: Request, actor: Actor):
    async with request.app.state.resources.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT * FROM business_proposals WHERE tenant_id=:tenant AND ((customer_id=:owner) OR (:supervisor AND status='CONFIRMED' AND requires_supervisor)) ORDER BY created_at DESC LIMIT 100"
                    ),
                    {
                        "tenant": actor.tenant_id,
                        "owner": actor.id,
                        "supervisor": actor.role == "supervisor",
                    },
                )
            )
            .mappings()
            .all()
        )
    return {"items": [visible(row) for row in rows]}


@router.get("/approvals/{operation_id}")
async def get_approval(operation_id: uuid.UUID, request: Request, actor: Actor):
    async with request.app.state.resources.engine.connect() as conn:
        row = await one(
            conn,
            "SELECT * FROM business_proposals WHERE operation_id=:id AND tenant_id=:tenant AND (customer_id=:owner OR :supervisor)",
            {
                "id": str(operation_id),
                "tenant": actor.tenant_id,
                "owner": actor.id,
                "supervisor": actor.role == "supervisor",
            },
        )
    if not row:
        raise HTTPException(404, "approval_not_found")
    return visible(row)


@router.get("/operations/{operation_id}")
async def get_operation(operation_id: uuid.UUID, request: Request, actor: Actor):
    async with request.app.state.resources.engine.connect() as conn:
        row = await one(
            conn,
            "SELECT b.operation_id,b.status,b.receipt,b.completed_at FROM business_operations b JOIN business_proposals p USING(operation_id) WHERE b.operation_id=:id AND p.tenant_id=:tenant AND (p.customer_id=:owner OR :supervisor)",
            {
                "id": str(operation_id),
                "tenant": actor.tenant_id,
                "owner": actor.id,
                "supervisor": actor.role == "supervisor",
            },
        )
    if not row:
        raise HTTPException(404, "operation_not_found")
    return dict(row)


@router.post("/approvals/{operation_id}/decision")
async def decide(operation_id: uuid.UUID, body: Decision, request: Request, actor: Actor):
    invalidated = False
    async with request.app.state.resources.engine.begin() as conn:
        pre = await one(
            conn,
            "SELECT * FROM business_proposals WHERE operation_id=:id AND tenant_id=:tenant AND (customer_id=:owner OR :supervisor)",
            {
                "id": str(operation_id),
                "tenant": actor.tenant_id,
                "owner": actor.id,
                "supervisor": actor.role == "supervisor",
            },
        )
        if not pre:
            raise HTTPException(404, "approval_not_found")
        run = await one(
            conn, "SELECT * FROM agent_runs WHERE id=:id FOR UPDATE", {"id": pre["workflow_id"]}
        )
        account = await one(
            conn, "SELECT * FROM customer_accounts WHERE id=:id FOR SHARE", {"id": actor.id}
        )
        policy = await one(
            conn,
            "SELECT * FROM merchant_policies WHERE tenant_id=:tenant FOR SHARE",
            {"tenant": actor.tenant_id},
        )
        order = await one(
            conn, "SELECT * FROM commerce_orders WHERE id=:id FOR SHARE", {"id": pre["order_id"]}
        )
        row = await one(
            conn,
            "SELECT * FROM business_proposals WHERE operation_id=:id FOR UPDATE",
            {"id": str(operation_id)},
        )
        if (
            not account["active"]
            or account["role"] != actor.role
            or account["tenant_id"] != actor.tenant_id
        ):
            raise HTTPException(403, "permission_revoked")
        if run["status"] != "WAITING_APPROVAL" or run["cancel_requested"]:
            raise HTTPException(409, "run_not_waiting_approval")
        if row["revision"] != body.expected_revision:
            raise HTTPException(409, "stale_proposal_revision")
        if row["status"] not in {"PENDING", "CONFIRMED"}:
            raise HTTPException(409, "proposal_already_decided")
        customer = row["customer_id"] == actor.id and actor.role == "customer"
        supervisor = (
            actor.role == "supervisor"
            and row["requires_supervisor"]
            and row["status"] == "CONFIRMED"
        )
        if (
            (body.decision == "confirm" and not customer)
            or (body.decision == "approve" and not supervisor)
            or (body.decision == "reject" and not (customer or supervisor))
        ):
            raise HTTPException(403, "decision_role_not_allowed")
        invalidated = (
            row["entity_version"] != order["version"]
            or row["policy_version"] != policy["version"]
            or row["expires_at"] <= datetime.now(UTC)
            or order["customer_id"] != row["customer_id"]
        )
        status = (
            "INVALIDATED"
            if invalidated
            else "REJECTED"
            if body.decision == "reject"
            else "APPROVED"
            if body.decision == "approve" or not row["requires_supervisor"]
            else "CONFIRMED"
        )
        await conn.execute(
            text(
                "UPDATE business_proposals SET status=:status,revision=revision+1,confirmed_by=CASE WHEN :decision='confirm' AND NOT :invalid THEN :actor ELSE confirmed_by END,approved_by=CASE WHEN :decision='approve' AND NOT :invalid THEN :actor ELSE approved_by END WHERE operation_id=:id"
            ),
            {
                "status": status,
                "decision": body.decision,
                "invalid": invalidated,
                "actor": actor.id,
                "id": str(operation_id),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO approval_audit(operation_id,actor_id,decision,proposal_revision) VALUES(:id,:actor,:decision,:revision)"
            ),
            {
                "id": str(operation_id),
                "actor": actor.id,
                "decision": status,
                "revision": row["revision"],
            },
        )
        if status in {"APPROVED", "REJECTED", "INVALIDATED"}:
            await conn.execute(
                text(
                    "UPDATE agent_runs SET status='QUEUED',resume_input=CAST(:resume AS jsonb),generation=generation+1,invocation_id=:invocation,revision=revision+1,updated_at=now() WHERE id=:id"
                ),
                {
                    "resume": json.dumps({"operation_id": str(operation_id)}),
                    "invocation": str(uuid.uuid4()),
                    "id": run["id"],
                },
            )
        else:
            await conn.execute(
                text("UPDATE agent_runs SET revision=revision+1,updated_at=now() WHERE id=:id"),
                {"id": run["id"]},
            )
    if invalidated:
        raise HTTPException(409, "proposal_invalidated_reconfirm_new_intent")
    return await get_approval(operation_id, request, actor)
