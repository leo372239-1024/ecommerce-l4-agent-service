"""Only reviewed typed DAGs can be reused. No uploaded prompt, shell or Python is executed."""

import json
import uuid

from fastapi import HTTPException
from sqlalchemy import text

from app.agent.contracts import Goal, TaskSpec, compile_plan
from app.business.catalog import CATALOG
from app.context.contracts import fingerprint
from app.context.retrieval import audit, namespace

GENERATOR = "workflow-v1"
SUPPORTED = {
    Goal.ORDER,
    Goal.LOGISTICS,
    Goal.ELIGIBILITY,
    Goal.QUOTE,
    Goal.REFUND,
    Goal.RETURN,
    Goal.CANCEL,
}


def tool_versions():
    return {
        key: {"version": spec.version, "schema_hash": spec.schema_hash}
        for key, spec in CATALOG.items()
    }


def make_template(goals):
    spec = TaskSpec(order_ids=["BOUND_ORDER"], goals=[Goal(g) for g in goals])
    if not set(spec.goals) <= SUPPORTED:
        raise ValueError("workflow_scope_not_supported")
    return {
        "generator_version": GENERATOR,
        "goals": sorted(g.value for g in spec.goals),
        "required_slots": ["order_id"],
        "dag": compile_plan(spec).model_dump(mode="json"),
        "failure_boundary": "正常规划、权限、当前政策、用户确认、主管审批和写后对账始终必需。",
    }


def instantiate(template, spec):
    canonical = make_template(template["goals"])
    if template != canonical or set(spec.goals) != {Goal(g) for g in template["goals"]}:
        raise ValueError("workflow_template_drift")
    if spec.queries or spec.missing_inputs or not spec.order_ids:
        raise ValueError("workflow_slots_unbound")
    if any(set(item.goals) != set(spec.goals) for item in spec.order_goals):
        raise ValueError("workflow_per_order_scope_mismatch")
    # Materialize only approved dependencies. The normal Plan compiler revalidates every guard.
    nodes = []
    for order in spec.order_ids:
        for node in template["dag"]["nodes"]:
            value = dict(node)
            value["id"] = order + node["id"][len("BOUND_ORDER") :]
            value["order_id"] = order
            value["dependencies"] = [
                order + key[len("BOUND_ORDER") :] for key in node["dependencies"]
            ]
            nodes.append(value)
    from app.agent.contracts import Plan

    result = Plan.model_validate_json(json.dumps({"version": 1, "nodes": nodes}))
    if result != compile_plan(spec):
        raise ValueError("workflow_mandatory_plan_mismatch")
    return result


class WorkflowService:
    def __init__(self, settings, engine):
        self.settings, self.engine = settings, engine

    async def candidate(self, actor, run_id):
        # Customers can nominate only their own successful verified development trajectories.
        if actor.role != "customer":
            raise HTTPException(403, "customer_trace_owner_required")
        async with self.engine.begin() as conn:
            run = (
                (
                    await conn.execute(
                        text(
                            "SELECT r.* FROM agent_runs r JOIN customer_accounts a ON a.id=r.customer_id "
                            "WHERE r.id=:run AND r.customer_id=:owner AND a.tenant_id=:tenant AND r.status='SUCCEEDED'"
                        ),
                        {**namespace(actor), "run": str(run_id)},
                    )
                )
                .mappings()
                .first()
            )
            if not run:
                raise HTTPException(404, "successful_owned_run_not_found")
            spec_raw = await conn.scalar(
                text(
                    "SELECT payload->'value' FROM agent_events WHERE workflow_id=:run AND payload->>'name' "
                    "IN ('business.task_spec','business.task_spec.resolved') ORDER BY seq DESC LIMIT 1"
                ),
                {"run": str(run_id)},
            )
            plan_event = await conn.scalar(
                text(
                    "SELECT payload FROM agent_events WHERE workflow_id=:run "
                    "AND payload->>'name'='business.plan' ORDER BY seq DESC LIMIT 1"
                ),
                {"run": str(run_id)},
            )
            plan_raw = (plan_event or {}).get("value")
            snapshot = (plan_event or {}).get("metadata", {})
            verified = (
                (
                    await conn.execute(
                        text(
                            "SELECT DISTINCT payload->'value'->>'node_id' FROM agent_events WHERE workflow_id=:run "
                            "AND payload->>'name'='business.verification' AND payload->'value'->>'passed'='true'"
                        ),
                        {"run": str(run_id)},
                    )
                )
                .scalars()
                .all()
            )
            if not spec_raw or not plan_raw:
                raise HTTPException(422, "trace_contract_missing")
            if (
                snapshot.get("dataset_partition") != "development"
                or not snapshot.get("tool_versions")
                or not snapshot.get("policy_version")
            ):
                raise HTTPException(422, "versioned_development_trace_required")
            spec = TaskSpec.model_validate_json(json.dumps(spec_raw))
            if spec.queries or not spec.order_ids or not set(spec.goals) <= SUPPORTED:
                raise HTTPException(422, "workflow_scope_not_supported")
            if any(set(item.goals) != set(spec.goals) for item in spec.order_goals):
                raise HTTPException(422, "workflow_per_order_scope_mismatch")
            if (
                set(verified) != {n["id"] for n in plan_raw["nodes"]}
                or compile_plan(spec).model_dump(mode="json") != plan_raw
            ):
                raise HTTPException(422, "trace_verification_incomplete")
            template = make_template(spec.goals)
            policy = await conn.scalar(
                text("SELECT version FROM merchant_policies WHERE tenant_id=:tenant"),
                namespace(actor),
            )
            if snapshot["tool_versions"] != tool_versions() or snapshot["policy_version"] != policy:
                raise HTTPException(409, "source_trace_version_drift")
            key = str(uuid.uuid4())
            actual = await conn.scalar(
                text(
                    "INSERT INTO workflow_memories(id,tenant_id,owner_id,source_run_id,template,fingerprint,tool_versions,policy_version) "
                    "VALUES(:id,:tenant,:owner,:run,CAST(:template AS jsonb),:hash,CAST(:versions AS jsonb),:policy) "
                    "ON CONFLICT(tenant_id,owner_id,source_run_id) DO UPDATE SET source_run_id=EXCLUDED.source_run_id RETURNING id"
                ),
                {
                    **namespace(actor),
                    "id": key,
                    "run": str(run_id),
                    "template": json.dumps(template),
                    "hash": fingerprint(template),
                    "versions": json.dumps(tool_versions()),
                    "policy": policy,
                },
            )
            await audit(conn, actor, actual, "workflow.candidate")
        return {"id": str(actual), "status": "CANDIDATE"}

    async def list(self, actor):
        async with self.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id,owner_id,source_run_id,template,fingerprint,status,regression,reviewed_by,expires_at "
                            "FROM workflow_memories WHERE tenant_id=:tenant AND (owner_id=:owner OR :role='supervisor') "
                            "ORDER BY created_at DESC LIMIT 100"
                        ),
                        namespace(actor),
                    )
                )
                .mappings()
                .all()
            )
        return {"items": [dict(row) for row in rows]}

    async def review(self, actor, key, action):
        if actor.role != "supervisor":
            raise HTTPException(403, "supervisor_review_required")
        async with self.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM workflow_memories WHERE id=:id AND tenant_id=:tenant FOR UPDATE"
                        ),
                        {**namespace(actor), "id": str(key)},
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                raise HTTPException(404, "workflow_not_found")
            policy = await conn.scalar(
                text("SELECT version FROM merchant_policies WHERE tenant_id=:tenant"),
                namespace(actor),
            )
            current = row["tool_versions"] == tool_versions() and row["policy_version"] == policy
            if action == "disable":
                status, regression = "DISABLED", row["regression"]
            elif not current or row["fingerprint"] != fingerprint(row["template"]):
                raise HTTPException(409, "workflow_version_drift")
            elif action == "validate":
                if row["status"] not in {"CANDIDATE", "VALIDATED"}:
                    raise HTTPException(409, "workflow_invalid_transition")
                try:
                    for count in (1, 2, 3):
                        spec = TaskSpec(
                            order_ids=[f"CASE-{i}" for i in range(count)],
                            goals=[Goal(g) for g in row["template"]["goals"]],
                        )
                        instantiate(row["template"], spec)
                except (ValueError, KeyError):
                    raise HTTPException(422, "workflow_regression_failed") from None
                status = "VALIDATED"
                regression = {
                    "passed": 3,
                    "kind": "structural_replay",
                    "generator": GENERATOR,
                    "template_hash": row["fingerprint"],
                    "business_benchmark": False,
                }
            elif action == "approve":
                if (
                    row["status"] != "VALIDATED"
                    or not row["regression"]
                    or row["regression"].get("template_hash") != row["fingerprint"]
                    or row["regression"].get("passed") != 3
                ):
                    raise HTTPException(409, "validated_regression_required")
                if row["expires_at"].timestamp() <= __import__("time").time():
                    raise HTTPException(409, "workflow_expired")
                status, regression = "APPROVED", row["regression"]
            else:
                raise HTTPException(422, "unknown_workflow_action")
            await conn.execute(
                text(
                    "UPDATE workflow_memories SET status=:status,regression=CAST(:regression AS jsonb),reviewed_by=:owner WHERE id=:id"
                ),
                {
                    **namespace(actor),
                    "id": str(key),
                    "status": status,
                    "regression": json.dumps(regression),
                },
            )
            await audit(conn, actor, key, "workflow." + action)
        return {"id": str(key), "status": status, "regression": regression}

    async def match(self, actor, spec):
        async with self.engine.begin() as conn:
            policy = await conn.scalar(
                text("SELECT version FROM merchant_policies WHERE tenant_id=:tenant"),
                namespace(actor),
            )
            # Immediately disable incompatible versions, even before another human review.
            await conn.execute(
                text(
                    "UPDATE workflow_memories SET status='DISABLED' WHERE tenant_id=:tenant AND status='APPROVED' "
                    "AND (policy_version!=:policy OR tool_versions!=CAST(:versions AS jsonb) OR expires_at<=now())"
                ),
                {**namespace(actor), "policy": policy, "versions": json.dumps(tool_versions())},
            )
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id,template,fingerprint FROM workflow_memories WHERE tenant_id=:tenant AND owner_id=:owner "
                            "AND status='APPROVED' AND expires_at>now() ORDER BY created_at DESC LIMIT 20"
                        ),
                        namespace(actor),
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            try:
                if fingerprint(row["template"]) != row["fingerprint"]:
                    continue
                return instantiate(row["template"], spec), {
                    "workflow_id": str(row["id"]),
                    "fingerprint": row["fingerprint"],
                    "publisher": "controlled-merchant",
                    "version": GENERATOR,
                }
            except (ValueError, KeyError):
                continue
        return None, None

    async def export(self, actor, key):
        # Export is a publication boundary too: stale approved material must not escape.
        async with self.engine.begin() as conn:
            policy = await conn.scalar(
                text("SELECT version FROM merchant_policies WHERE tenant_id=:tenant"),
                namespace(actor),
            )
            await conn.execute(
                text(
                    "UPDATE workflow_memories SET status='DISABLED' WHERE id=:id AND tenant_id=:tenant "
                    "AND (owner_id=:owner OR :role='supervisor') AND status='APPROVED' "
                    "AND (policy_version!=:policy OR tool_versions!=CAST(:versions AS jsonb) OR expires_at<=now())"
                ),
                {
                    **namespace(actor),
                    "id": str(key),
                    "policy": policy,
                    "versions": json.dumps(tool_versions()),
                },
            )
        rows = (await self.list(actor))["items"]
        row = next((r for r in rows if str(r["id"]) == str(key)), None)
        if (
            not row
            or row["status"] != "APPROVED"
            or row["fingerprint"] != fingerprint(row["template"])
        ):
            raise HTTPException(404, "approved_workflow_not_found")
        return {
            "filename": "SKILL.md",
            "content": "---\nname: ecommerce-workflow-"
            + str(key)
            + "\ndescription: Reviewed typed e-commerce workflow\n---\n\nPublisher: controlled-merchant\nVersion: "
            + GENERATOR
            + "\nContent hash: "
            + row["fingerprint"]
            + "\n\nFollow the gateway's current authorization, policy, confirmation and reconciliation contracts.\n\n```json\n"
            + json.dumps(row["template"], ensure_ascii=False, indent=2)
            + "\n```\n",
        }
