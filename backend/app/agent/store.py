import json
import uuid

from fastapi import HTTPException
from sqlalchemy import text

from app.security.guards import safe_output

TERMINAL = {"SUCCEEDED", "PARTIAL", "HANDOFF", "FAILED", "CANCELLED"}


class LeaseLost(RuntimeError):
    pass


class RunCancelled(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class RunStore:
    def __init__(self, engine):
        self.engine = engine

    async def create(
        self, owner, message, key, request_hash, partition="development", conversation_id=None
    ):
        run_id, invocation = str(uuid.uuid4()), str(uuid.uuid4())
        async with self.engine.begin() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:owner,0))"),
                {"owner": "admission:" + owner},
            )
            existing = await conn.scalar(
                text("SELECT id FROM agent_runs WHERE customer_id=:owner AND request_key=:key"),
                {"owner": owner, "key": key},
            )
            if existing:
                row = (
                    (
                        await conn.execute(
                            text("SELECT * FROM agent_runs WHERE id=:id AND customer_id=:owner"),
                            {"id": existing, "owner": owner},
                        )
                    )
                    .mappings()
                    .one()
                )
                if (
                    row["request_hash"] != request_hash
                    or row["dataset_partition"] != partition
                    or row["message"] != message
                    or str(row.get("conversation_id") or "") != str(conversation_id or "")
                ):
                    raise HTTPException(409, "idempotency_key_content_conflict")
                return dict(row)
            pending = await conn.scalar(
                text(
                    "SELECT count(*) FROM agent_runs WHERE customer_id=:owner AND status IN ('QUEUED','RUNNING','WAITING_USER','WAITING_APPROVAL')"
                ),
                {"owner": owner},
            )
            if not existing and pending >= 5:
                raise HTTPException(429, "customer_run_limit", headers={"Retry-After": "5"})
            conversation_seq = None
            if conversation_id and not existing:
                conversation = (
                    (
                        await conn.execute(
                            text(
                                "SELECT next_seq FROM agent_conversations WHERE id=:id AND customer_id=:owner "
                                "AND deleted_at IS NULL AND expires_at>now() FOR UPDATE"
                            ),
                            {"id": str(conversation_id), "owner": owner},
                        )
                    )
                    .mappings()
                    .first()
                )
                if not conversation:
                    raise HTTPException(404, "conversation_not_found")
                if conversation["next_seq"] > 200:
                    raise HTTPException(409, "conversation_turn_limit")
                busy = await conn.scalar(
                    text(
                        "SELECT count(*) FROM agent_runs WHERE conversation_id=:id AND status IN "
                        "('QUEUED','RUNNING','WAITING_USER','WAITING_APPROVAL')"
                    ),
                    {"id": str(conversation_id)},
                )
                if busy:
                    raise HTTPException(409, "conversation_task_in_progress")
                conversation_seq = conversation["next_seq"]
                await conn.execute(
                    text("UPDATE agent_conversations SET next_seq=next_seq+1 WHERE id=:id"),
                    {"id": str(conversation_id)},
                )
            await conn.execute(
                text(
                    "INSERT INTO agent_runs (id,customer_id,message,request_key,request_hash,status,invocation_id,dataset_partition,conversation_id,conversation_seq) "
                    "VALUES (:id,:owner,:message,:key,:hash,'QUEUED',:invocation,:partition,:conversation,:seq) "
                    "ON CONFLICT (customer_id,request_key) DO NOTHING"
                ),
                {
                    "id": run_id,
                    "owner": owner,
                    "message": message,
                    "key": key,
                    "hash": request_hash,
                    "invocation": invocation,
                    "partition": partition,
                    "conversation": str(conversation_id) if conversation_id else None,
                    "seq": conversation_seq,
                },
            )
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM agent_runs WHERE customer_id=:owner AND request_key=:key"
                        ),
                        {"owner": owner, "key": key},
                    )
                )
                .mappings()
                .one()
            )
            if (
                row["request_hash"] != request_hash
                or row["dataset_partition"] != partition
                or row["message"] != message
                or str(row.get("conversation_id") or "") != str(conversation_id or "")
            ):
                raise HTTPException(409, "idempotency_key_content_conflict")
            if str(row["id"]) == run_id:
                await self.append_locked(
                    conn,
                    row,
                    "queued",
                    {
                        "type": "CUSTOM",
                        "name": "business.run.status",
                        "value": {"status": "QUEUED"},
                    },
                )
        return dict(row)

    async def get(self, run_id, owner=None):
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM agent_runs WHERE id=:id"
                            + (" AND customer_id=:owner" if owner else "")
                        ),
                        {"id": str(run_id), "owner": owner},
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            raise HTTPException(404, "run_not_found")
        return dict(row)

    async def claim(self, worker_id, lease_seconds, run_id=None):
        async with self.engine.begin() as conn:
            return (
                (
                    await conn.execute(
                        text("""
                WITH candidate AS (
                  SELECT id FROM agent_runs WHERE (status='QUEUED'
                     OR (status='RUNNING' AND lease_until<now()))
                     AND (CAST(:run_id AS uuid) IS NULL OR id=CAST(:run_id AS uuid))
                  ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
                ) UPDATE agent_runs r SET status='RUNNING',lease_owner=:worker,
                    active_seconds=active_seconds+CASE WHEN active_accounted_at IS NULL THEN 0
                      ELSE greatest(0,extract(epoch FROM (least(now(),lease_until)-active_accounted_at))) END,
                    active_accounted_at=now(),
                    lease_until=now()+make_interval(secs=>:seconds),fencing=fencing+1,
                    revision=revision+1,updated_at=now()
                  FROM candidate WHERE r.id=candidate.id RETURNING r.*
            """),
                        {
                            "worker": worker_id,
                            "seconds": lease_seconds,
                            "run_id": str(run_id) if run_id else None,
                        },
                    )
                )
                .mappings()
                .first()
            )

    async def check(self, run_id, fence, renew_seconds=None, active_limit=180):
        async with self.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT status,cancel_requested,fencing,lease_until>now() AS valid, "
                            "active_seconds+greatest(0,extract(epoch FROM (now()-active_accounted_at))) AS active "
                            "FROM agent_runs WHERE id=:id FOR UPDATE"
                        ),
                        {"id": str(run_id)},
                    )
                )
                .mappings()
                .one()
            )
            if row["fencing"] != fence or row["status"] != "RUNNING" or not row["valid"]:
                raise LeaseLost("lease_lost")
            if row["cancel_requested"]:
                raise RunCancelled("run_cancelled")
            if row["active"] >= active_limit:
                raise BudgetExceeded("active_time_budget")
            if renew_seconds:
                await conn.execute(
                    text(
                        "UPDATE agent_runs SET active_seconds=active_seconds+greatest(0,extract(epoch FROM (now()-active_accounted_at))), "
                        "active_accounted_at=now(),lease_until=now()+make_interval(secs=>:seconds) WHERE id=:id"
                    ),
                    {"id": str(run_id), "seconds": renew_seconds},
                )

    async def append_locked(self, conn, row, key, payload):
        payload = safe_output(payload)
        exists = await conn.scalar(
            text("SELECT seq FROM agent_events WHERE workflow_id=:id AND event_key=:key"),
            {"id": str(row["id"]), "key": key},
        )
        if exists is not None:
            return exists
        seq = await conn.scalar(
            text("UPDATE agent_runs SET event_seq=event_seq+1 WHERE id=:id RETURNING event_seq"),
            {"id": str(row["id"])},
        )
        await conn.execute(
            text("INSERT INTO agent_events VALUES (:id,:seq,:key,CAST(:payload AS jsonb),now())"),
            {
                "id": str(row["id"]),
                "seq": seq,
                "key": key,
                "payload": json.dumps(payload, ensure_ascii=False),
            },
        )
        await conn.execute(
            text("INSERT INTO agent_outbox(workflow_id,seq) VALUES (:id,:seq)"),
            {"id": str(row["id"]), "seq": seq},
        )
        return seq

    async def emit(self, run_id, fence, key, event):
        async with self.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM agent_runs WHERE id=:id FOR UPDATE"),
                        {"id": str(run_id)},
                    )
                )
                .mappings()
                .one()
            )
            if row["cancel_requested"]:
                raise RunCancelled("run_cancelled")
            if row["fencing"] != fence or row["status"] != "RUNNING":
                raise LeaseLost("lease_lost")
            await self.append_locked(
                conn, row, key, event.model_dump(mode="json", by_alias=True, exclude_none=True)
            )

    async def finish(self, run_id, fence, status, result, terminal):
        result = safe_output(result)
        async with self.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM agent_runs WHERE id=:id FOR UPDATE"),
                        {"id": str(run_id)},
                    )
                )
                .mappings()
                .one()
            )
            if row["fencing"] != fence or row["status"] != "RUNNING":
                raise LeaseLost("lease_lost")
            # Cancel always wins against a late model/tool result.
            if row["cancel_requested"]:
                status, result = "CANCELLED", {"message": "任务已取消"}
                effects = (
                    (
                        await conn.execute(
                            text(
                                "SELECT b.receipt FROM business_operations b JOIN business_proposals p USING(operation_id) WHERE p.workflow_id=:id AND b.status='SUCCEEDED'"
                            ),
                            {"id": str(run_id)},
                        )
                    )
                    .scalars()
                    .all()
                )
                if effects:
                    status, result = (
                        "PARTIAL",
                        {
                            "message": "已停止后续执行；此前完成的业务操作仍然有效，请核对回执。",
                            "completed_operations": list(effects),
                        },
                    )
                terminal.outcome = None
            if status in {"HANDOFF", "PARTIAL"}:
                proposals = (
                    (
                        await conn.execute(
                            text(
                                "SELECT operation_id,order_id,action,tenant_id FROM business_proposals WHERE workflow_id=:id"
                            ),
                            {"id": str(run_id)},
                        )
                    )
                    .mappings()
                    .all()
                )
                plan = await conn.scalar(
                    text(
                        "SELECT payload->'value' FROM agent_events WHERE workflow_id=:id AND payload->>'name'='business.plan' ORDER BY seq DESC LIMIT 1"
                    ),
                    {"id": str(run_id)},
                )
                orders = sorted(
                    {node["order_id"] for node in (plan or {}).get("nodes", [])}
                    | {item["order_id"] for item in proposals}
                )
                tenant = (
                    proposals[0]["tenant_id"]
                    if proposals
                    else await conn.scalar(
                        text("SELECT tenant_id FROM customer_accounts WHERE id=:id"),
                        {"id": row["customer_id"]},
                    )
                )
                packet = {
                    "workflow_id": str(run_id),
                    "stop_status": status,
                    "error": result.get("error"),
                    "failures": result.get("failures", {}),
                    "requested_order_ids": orders,
                    "operations": [
                        {
                            "operation_id": str(item["operation_id"]),
                            "order_id": item["order_id"],
                            "action": item["action"],
                        }
                        for item in proposals
                    ],
                }
                case_id = str(uuid.uuid4())
                await conn.execute(
                    text(
                        "INSERT INTO handoff_cases(id,workflow_id,customer_id,tenant_id,order_ids,packet) VALUES(:case,:workflow,:owner,:tenant,:orders,CAST(:packet AS jsonb)) ON CONFLICT(workflow_id) DO NOTHING"
                    ),
                    {
                        "case": case_id,
                        "workflow": str(run_id),
                        "owner": row["customer_id"],
                        "tenant": tenant,
                        "orders": orders,
                        "packet": json.dumps(packet),
                    },
                )
                case_id = str(
                    await conn.scalar(
                        text("SELECT id FROM handoff_cases WHERE workflow_id=:id"),
                        {"id": str(run_id)},
                    )
                )
                result = {**result, "handoff_case_id": case_id}
                await self.append_locked(
                    conn,
                    row,
                    f"{row['invocation_id']}:handoff",
                    {
                        "type": "CUSTOM",
                        "name": "business.case.created",
                        "value": {"case_id": case_id, "status": "OPEN"},
                    },
                )
            result = {**result, "status": status}
            if status in TERMINAL and row["dataset_partition"] == "development":
                # Completion and memory scheduling commit together; background retries cannot
                # repeat a business effect and never store message/tool bodies in an episode.
                await conn.execute(
                    text(
                        "INSERT INTO memory_outbox(workflow_id) VALUES(:id) ON CONFLICT DO NOTHING"
                    ),
                    {"id": str(run_id)},
                )
            terminal.result = result
            await self.append_locked(
                conn,
                row,
                f"{row['invocation_id']}:terminal",
                terminal.model_dump(mode="json", by_alias=True, exclude_none=True),
            )
            await conn.execute(
                text(
                    "UPDATE agent_runs SET status=:status,result=CAST(:result AS jsonb),lease_owner=NULL,"
                    "lease_until=NULL,active_seconds=active_seconds+greatest(0,extract(epoch FROM (now()-active_accounted_at))), "
                    "active_accounted_at=NULL,resume_input=NULL,revision=revision+1,updated_at=now() WHERE id=:id"
                ),
                {
                    "status": status,
                    "result": json.dumps(result, ensure_ascii=False),
                    "id": str(run_id),
                },
            )

    async def transition(self, run_id, owner, expected_revision, action, resume=None):
        async with self.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM agent_runs WHERE id=:id AND customer_id=:owner FOR UPDATE"
                        ),
                        {"id": str(run_id), "owner": owner},
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                raise HTTPException(404, "run_not_found")
            if row["revision"] != expected_revision:
                raise HTTPException(409, "stale_run_revision")
            if action == "resume":
                if row["status"] != "WAITING_USER":
                    raise HTTPException(409, "run_not_waiting_user")
                await conn.execute(
                    text(
                        "UPDATE agent_runs SET status='QUEUED',resume_input=CAST(:resume AS jsonb),"
                        "generation=generation+1,invocation_id=:invocation,revision=revision+1,updated_at=now() WHERE id=:id"
                    ),
                    {
                        "id": str(run_id),
                        "resume": json.dumps(resume),
                        "invocation": str(uuid.uuid4()),
                    },
                )
            else:
                if row["status"] in TERMINAL:
                    raise HTTPException(409, "run_already_terminal")
                # A worker observes this at boundaries or in its lease heartbeat.
                new_status = "RUNNING" if row["status"] == "RUNNING" else "CANCELLED"
                await conn.execute(
                    text(
                        "UPDATE agent_runs SET status=:status,cancel_requested=true,revision=revision+1,"
                        "updated_at=now(),result=CASE WHEN :status='CANCELLED' THEN '{\"message\":\"任务已取消\"}'::jsonb ELSE result END "
                        "WHERE id=:id"
                    ),
                    {"id": str(run_id), "status": new_status},
                )
                if new_status == "CANCELLED":
                    await self.append_locked(
                        conn,
                        row,
                        f"cancel:{row['revision']}",
                        {
                            "type": "CUSTOM",
                            "name": "business.run.status",
                            "value": {"status": "CANCELLED"},
                        },
                    )
        return await self.get(run_id, owner)

    async def events(self, run_id, after, limit=200):
        async with self.engine.connect() as conn:
            return (
                (
                    await conn.execute(
                        text(
                            "SELECT seq,payload FROM agent_events WHERE workflow_id=:id AND seq>:seq ORDER BY seq LIMIT :limit"
                        ),
                        {"id": str(run_id), "seq": after, "limit": limit},
                    )
                )
                .mappings()
                .all()
            )

    async def event_payload(self, run_id, key):
        async with self.engine.connect() as conn:
            return await conn.scalar(
                text("SELECT payload FROM agent_events WHERE workflow_id=:id AND event_key=:key"),
                {"id": str(run_id), "key": key},
            )
