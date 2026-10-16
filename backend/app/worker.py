import asyncio
import signal
import socket
import uuid

import structlog
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from opentelemetry import trace
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.agent.planner import PlanningError
from app.agent.runtime import Harness
from app.agent.store import BudgetExceeded, LeaseLost, RunCancelled, RunStore
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.resources import Resources
from app.core.telemetry import configure_telemetry
from app.core.wakeups import EventWakeups
from app.init_schema import conninfo

logger = structlog.get_logger()


async def execute_run(settings, resources, row, harness_class=Harness):
    store, row = RunStore(resources.engine), dict(row)
    harness = harness_class(settings, store, row)
    execution = asyncio.current_task()
    reason = None

    async def renew():
        nonlocal reason
        try:
            while True:
                await asyncio.sleep(settings.worker_lease_seconds / 3)
                await store.check(
                    row["id"],
                    row["fencing"],
                    settings.worker_lease_seconds,
                    settings.run_active_seconds,
                )
        except (LeaseLost, RunCancelled, BudgetExceeded) as exc:
            reason = exc
            execution.cancel()
        except (SQLAlchemyError, OSError):
            reason = LeaseLost("lease_renewal_failed")
            execution.cancel()

    heartbeat = asyncio.create_task(renew())
    try:
        # The session lock serializes checkpointer access even if a lease changes owner.
        async with await AsyncConnection.connect(
            conninfo(settings), autocommit=True, prepare_threshold=0, row_factory=dict_row
        ) as conn:
            lock = await conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked", (str(row["id"]),)
            )
            if not (await lock.fetchone())["locked"]:
                return  # No graph access; this claim expires and is retried after the old owner exits.
            saver = AsyncPostgresSaver(conn)
            graph = harness.graph(saver)
            config = {"configurable": {"thread_id": str(row["id"])}, "recursion_limit": 200}
            await store.check(row["id"], row["fencing"], active_limit=settings.run_active_seconds)
            await harness.started()
            snapshot = await graph.aget_state(config)
            pending = [i for task in snapshot.tasks for i in task.interrupts]
            if not snapshot.values:
                graph_input = {"message": row["message"]}
            elif pending and row["resume_input"]:
                graph_input = Command(resume=row["resume_input"])
            else:
                graph_input = None
            remaining = max(0.001, settings.run_active_seconds - row["active_seconds"])
            with trace.get_tracer("agent-worker").start_as_current_span(
                "agent.run",
                attributes={
                    "agent.workflow_id": str(row["id"]),
                    "agent.invocation_id": str(row["invocation_id"]),
                    "agent.fencing": row["fencing"],
                },
            ):
                async with asyncio.timeout(remaining):
                    # A crash after final checkpoint must finalize the DB without redoing the graph.
                    if snapshot.values and not snapshot.next:
                        result = snapshot.values
                    else:
                        result = await graph.ainvoke(graph_input, config, durability="sync")
                interrupts = result.get("__interrupt__", ())
                if interrupts:
                    value = interrupts[0].value
                    await store.finish(
                        row["id"],
                        row["fencing"],
                        "WAITING_APPROVAL" if value.get("kind") == "approval" else "WAITING_USER",
                        value,
                        harness.terminal(value, interrupts[0]),
                    )
                else:
                    await store.finish(
                        row["id"],
                        row["fencing"],
                        result["status"],
                        result["result"],
                        harness.terminal(result["result"]),
                    )
    except asyncio.CancelledError:
        if isinstance(reason, RunCancelled):
            result = {"message": "任务已取消"}
            await store.finish(
                row["id"], row["fencing"], "CANCELLED", result, harness.terminal(result)
            )
        elif isinstance(reason, BudgetExceeded):
            result = {
                "message": "任务达到执行预算，已停止并转人工。",
                "error": "active_time_budget",
            }
            await store.finish(
                row["id"], row["fencing"], "HANDOFF", result, harness.terminal(result)
            )
        # Shutdown/lost lease must leave the durable job for another worker.
    except LeaseLost:
        logger.warning("run_lease_lost", workflow_id=str(row["id"]))
    except (RunCancelled, BudgetExceeded, PlanningError, TimeoutError) as exc:
        status = "CANCELLED" if isinstance(exc, RunCancelled) else "HANDOFF"
        code = str(exc) if isinstance(exc, (PlanningError, BudgetExceeded)) else type(exc).__name__
        result = {
            "message": "任务已停止，请人工处理。",
            "error": code,
            "usage": getattr(exc, "usage", {}),
        }
        try:
            await store.finish(row["id"], row["fencing"], status, result, harness.terminal(result))
        except LeaseLost:
            pass
    except Exception as exc:  # noqa: BLE001 - boundary keeps one failed run from killing the worker
        # Log only the exception class, never prompts, provider responses or credentials.
        logger.error(
            "run_execution_failed", workflow_id=str(row["id"]), error_type=type(exc).__name__
        )
        try:
            result = {"message": "任务执行失败，请稍后重试或转人工。", "error": "execution_failed"}
            await store.finish(
                row["id"], row["fencing"], "FAILED", result, harness.terminal(result)
            )
        except (LeaseLost, SQLAlchemyError):
            pass
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


async def publish_outbox(resources):
    async with resources.engine.begin() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT workflow_id,seq FROM agent_outbox WHERE delivered_at IS NULL "
                        "ORDER BY workflow_id,seq FOR UPDATE SKIP LOCKED LIMIT 100"
                    )
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            # Duplicate notifications are harmless: PostgreSQL seq is the consumer cursor.
            await resources.redis.publish("agent:events", f"{row['workflow_id']}:{row['seq']}")
            await conn.execute(
                text(
                    "UPDATE agent_outbox SET delivered_at=now() WHERE workflow_id=:id AND seq=:seq"
                ),
                {"id": row["workflow_id"], "seq": row["seq"]},
            )


async def run():
    settings = get_settings()
    configure_logging(settings.log_level)
    provider = configure_telemetry(settings)
    if provider:
        trace.set_tracer_provider(provider)
    resources, stop = Resources(settings), asyncio.Event()
    wakeups = EventWakeups(resources.redis)
    wakeups.start()
    worker_id = socket.gethostname() + ":" + uuid.uuid4().hex[:8]
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    tasks = set()
    store = RunStore(resources.engine)

    async def infra_heartbeat():
        while not stop.is_set():
            try:
                async with resources.engine.begin() as conn:
                    await conn.execute(
                        text(
                            "INSERT INTO worker_heartbeats VALUES (:id,now(),'ready') ON CONFLICT (worker_id) "
                            "DO UPDATE SET last_seen=now(),status='ready'"
                        ),
                        {"id": worker_id},
                    )
                await resources.redis.set(f"infra:worker:{socket.gethostname()}", "ready", ex=20)
                await publish_outbox(resources)
                from app.business.service import MerchantService

                await MerchantService(resources.engine).consume_events()
            except (SQLAlchemyError, RedisError, TimeoutError, OSError):
                logger.warning("worker_notification_dependency_unavailable")
            try:
                await asyncio.wait_for(stop.wait(), 2)
            except TimeoutError:
                pass

    heartbeat = asyncio.create_task(infra_heartbeat())
    from app.context.jobs import ContextJobs

    context_jobs = asyncio.create_task(ContextJobs(settings, resources.engine).run(stop))
    logger.info(
        "worker_started",
        worker_id=worker_id,
        capability="controlled_merchant_agent"
        if settings.tool_routing_enabled
        else "read_only_agent_core",
    )
    try:
        while not stop.is_set():
            tasks = {task for task in tasks if not task.done()}
            if len(tasks) < settings.worker_concurrency:
                try:
                    row = await store.claim(worker_id, settings.worker_lease_seconds)
                    if row:
                        tasks.add(asyncio.create_task(execute_run(settings, resources, row)))
                        continue
                except (SQLAlchemyError, OSError):
                    logger.warning("worker_database_unavailable")
            try:
                await wakeups.wait(timeout=0.5)
            except TimeoutError:
                pass
    finally:
        # Allow in-flight read steps to checkpoint; hard kill is covered by lease recovery.
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=20)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        context_jobs.cancel()
        await asyncio.gather(context_jobs, return_exceptions=True)
        await wakeups.close()
        try:
            await resources.redis.delete(f"infra:worker:{socket.gethostname()}")
        except (RedisError, TimeoutError):
            pass
        await resources.close()
        if provider:
            provider.shutdown()
        logger.info("worker_stopped", worker_id=worker_id)


if __name__ == "__main__":
    asyncio.run(run())
