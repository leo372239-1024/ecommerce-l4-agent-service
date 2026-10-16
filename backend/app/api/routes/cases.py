import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, field_validator
from sqlalchemy import text

from app.agent.contracts import StrictModel
from app.business.service import one
from app.core.auth import Principal, principal
from app.security.guards import redact_text, safe_output

router = APIRouter()
Actor = Annotated[Principal, Depends(principal)]


class Revision(StrictModel):
    expected_revision: int = Field(ge=1)


class Closure(Revision):
    resolution: str = Field(min_length=3, max_length=1000)

    @field_validator("resolution")
    @classmethod
    def safe_resolution(cls, value):
        return redact_text(value)


def staff(actor):
    if actor.role not in {"support", "supervisor"}:
        raise HTTPException(403, "staff_role_required")


async def owned_case(conn, case_id, actor, revision, *, claimed=False):
    staff(actor)
    row = await one(
        conn,
        "SELECT * FROM handoff_cases WHERE id=:id AND tenant_id=:tenant FOR UPDATE",
        {"id": str(case_id), "tenant": actor.tenant_id},
    )
    current = await one(
        conn,
        "SELECT role,tenant_id,active FROM customer_accounts WHERE id=:id FOR SHARE",
        {"id": actor.id},
    )
    if (
        not current
        or not current["active"]
        or current["role"] != actor.role
        or current["tenant_id"] != actor.tenant_id
    ):
        raise HTTPException(403, "staff_authority_revoked")
    if not row:
        raise HTTPException(404, "case_not_found")
    if row["revision"] != revision:
        raise HTTPException(409, "stale_case_revision")
    if claimed and (row["status"] != "CLAIMED" or row["assigned_to"] != actor.id):
        raise HTTPException(409, "case_not_assigned_to_actor")
    return row


async def audit(conn, row, actor, action):
    await conn.execute(
        text(
            "INSERT INTO case_audit(case_id,actor_id,action,revision) VALUES(:id,:actor,:action,:revision)"
        ),
        {"id": row["id"], "actor": actor.id, "action": action, "revision": row["revision"]},
    )


@router.get("/cases")
async def list_cases(request: Request, actor: Actor):
    async with request.app.state.resources.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT id,workflow_id,status,revision,assigned_to,created_at,closed_at FROM handoff_cases WHERE tenant_id=:tenant AND (customer_id=:owner OR :staff) ORDER BY created_at DESC LIMIT 100"
                    ),
                    {
                        "tenant": actor.tenant_id,
                        "owner": actor.id,
                        "staff": actor.role in {"support", "supervisor"},
                    },
                )
            )
            .mappings()
            .all()
        )
    return {"items": [dict(row) for row in rows]}


@router.get("/cases/{case_id}")
async def get_case(case_id: UUID, request: Request, actor: Actor):
    async with request.app.state.resources.engine.connect() as conn:
        row = await one(
            conn,
            "SELECT * FROM handoff_cases WHERE id=:id AND tenant_id=:tenant AND (customer_id=:owner OR :staff)",
            {
                "id": str(case_id),
                "tenant": actor.tenant_id,
                "owner": actor.id,
                "staff": actor.role in {"support", "supervisor"},
            },
        )
        operations = []
        if row:
            operations = (
                (
                    await conn.execute(
                        text(
                            "SELECT p.operation_id,p.order_id,p.action,p.status AS proposal_status,"
                            "b.status AS execution_status,b.receipt,b.completed_at "
                            "FROM business_proposals p LEFT JOIN business_operations b USING(operation_id) "
                            "WHERE p.workflow_id=:workflow AND p.tenant_id=:tenant ORDER BY p.created_at"
                        ),
                        {"workflow": row["workflow_id"], "tenant": actor.tenant_id},
                    )
                )
                .mappings()
                .all()
            )
    if not row:
        raise HTTPException(404, "case_not_found")
    return safe_output({**dict(row), "operation_receipts": [dict(item) for item in operations]})


@router.post("/cases/{case_id}/claim")
async def claim_case(case_id: UUID, body: Revision, request: Request, actor: Actor):
    async with request.app.state.resources.engine.begin() as conn:
        row = await owned_case(conn, case_id, actor, body.expected_revision)
        if row["status"] != "OPEN":
            raise HTTPException(409, "case_not_open")
        await conn.execute(
            text(
                "UPDATE handoff_cases SET status='CLAIMED',assigned_to=:actor,revision=revision+1 WHERE id=:id"
            ),
            {"actor": actor.id, "id": row["id"]},
        )
        await audit(conn, row, actor, "CLAIM")
    return await get_case(case_id, request, actor)


@router.post("/cases/{case_id}/reconcile")
async def reconcile_case(case_id: UUID, body: Revision, request: Request, actor: Actor):
    # This proof applies to this simulator's shared transaction/fencing contract ONLY.
    # A real merchant adapter must query its authoritative status; a local timeout is no proof.
    staff(actor)
    async with request.app.state.resources.engine.begin() as conn:
        pre = await one(
            conn,
            "SELECT workflow_id FROM handoff_cases WHERE id=:id AND tenant_id=:tenant",
            {"id": str(case_id), "tenant": actor.tenant_id},
        )
        if not pre:
            raise HTTPException(404, "case_not_found")
        run = await one(
            conn,
            "SELECT status FROM agent_runs WHERE id=:id FOR UPDATE",
            {"id": pre["workflow_id"]},
        )
        row = await owned_case(conn, case_id, actor, body.expected_revision, claimed=True)
        if run["status"] not in {"HANDOFF", "PARTIAL", "FAILED", "CANCELLED", "SUCCEEDED"}:
            raise HTTPException(409, "execution_not_stopped")
        pending = (
            (
                await conn.execute(
                    text(
                        "SELECT b.operation_id FROM business_operations b JOIN business_proposals p USING(operation_id) WHERE p.workflow_id=:id AND p.tenant_id=:tenant AND b.status IN ('PREPARED','DISPATCHED','RECONCILING') FOR UPDATE OF b"
                    ),
                    {"id": row["workflow_id"], "tenant": actor.tenant_id},
                )
            )
            .scalars()
            .all()
        )
        for operation in pending:
            # Run row lock waits for any in-flight simulator transaction; terminal status
            # prohibits future dispatch, and absence of a committed receipt proves no effect.
            await conn.execute(
                text(
                    "UPDATE business_operations SET status='REJECTED',receipt=CAST(:receipt AS jsonb),completed_at=now() WHERE operation_id=:id"
                ),
                {
                    "id": operation,
                    "receipt": json.dumps(
                        {
                            "error": "authoritatively_uncommitted",
                            "source": "shared_merchant_transaction",
                            "simulated_business": True,
                        }
                    ),
                },
            )
        await conn.execute(
            text("UPDATE handoff_cases SET revision=revision+1 WHERE id=:id"), {"id": row["id"]}
        )
        await audit(conn, row, actor, "RECONCILE")
    return await get_case(case_id, request, actor)


@router.post("/cases/{case_id}/close")
async def close_case(case_id: UUID, body: Closure, request: Request, actor: Actor):
    async with request.app.state.resources.engine.begin() as conn:
        row = await owned_case(conn, case_id, actor, body.expected_revision, claimed=True)
        pending = await conn.scalar(
            text(
                "SELECT count(*) FROM business_operations b JOIN business_proposals p USING(operation_id) WHERE p.workflow_id=:id AND b.status NOT IN ('SUCCEEDED','REJECTED')"
            ),
            {"id": row["workflow_id"]},
        )
        if pending:
            raise HTTPException(409, "unknown_operations_require_authoritative_reconciliation")
        await conn.execute(
            text(
                "UPDATE handoff_cases SET status='CLOSED',resolution=:resolution,closed_at=now(),revision=revision+1 WHERE id=:id"
            ),
            {"resolution": body.resolution, "id": row["id"]},
        )
        await audit(conn, row, actor, "CLOSE")
    return await get_case(case_id, request, actor)
