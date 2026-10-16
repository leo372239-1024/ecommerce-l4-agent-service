"""Execute in API container while workers are stopped; scoped synthetic persistence tests."""

import asyncio
import json
import uuid

from ag_ui.core import CustomEvent, RunFinishedEvent
from fastapi import HTTPException
from psycopg import AsyncConnection
from sqlalchemy import text

from app.agent.store import BudgetExceeded, LeaseLost, RunStore
from app.core.config import get_settings
from app.core.resources import Resources
from app.init_schema import conninfo


async def main():
    resources = Resources(get_settings())
    store = RunStore(resources.engine)
    created, report = [], {}
    try:
        for _ in range(2):
            created.append(
                await store.create(
                    "customer-alice", "Persistence test only", uuid.uuid4().hex, "test"
                )
            )
        claims = await asyncio.gather(store.claim("probe-a", 30), store.claim("probe-b", 30))
        assert {row["id"] for row in claims} == {row["id"] for row in created}
        report["concurrent_skip_locked_claim"] = "passed"
        first = dict(claims[0])
        async with resources.engine.begin() as conn:
            await conn.execute(
                text("UPDATE agent_runs SET lease_until=now()-interval '1 second' WHERE id=:id"),
                {"id": first["id"]},
            )
        second = dict(await store.claim("probe-new-owner", 30))
        assert first["id"] == second["id"] and second["fencing"] == first["fencing"] + 1
        for operation in (
            store.emit(first["id"], first["fencing"], "stale", CustomEvent(name="probe", value={})),
            store.finish(
                first["id"],
                first["fencing"],
                "SUCCEEDED",
                {},
                RunFinishedEvent(thread_id=str(first["id"]), run_id=str(first["invocation_id"])),
            ),
        ):
            try:
                await operation
            except LeaseLost:
                pass
            else:
                raise AssertionError("stale_owner_accepted")
        report["monotonic_fencing_rejects_old_events_and_terminal"] = "passed"
        async with resources.engine.begin() as conn:
            await conn.execute(
                text("UPDATE agent_runs SET active_seconds=181 WHERE id=:id"), {"id": second["id"]}
            )
        try:
            await store.check(second["id"], second["fencing"])
        except BudgetExceeded:
            pass
        else:
            raise AssertionError("persisted_budget_ignored")
        report["persistent_active_budget"] = "passed"
        for row in (second, dict(claims[1])):
            await store.transition(
                row["id"], row["customer_id"], (await store.get(row["id"]))["revision"], "cancel"
            )
            await store.finish(
                row["id"],
                row["fencing"],
                "SUCCEEDED",
                {"message": "late result"},
                RunFinishedEvent(thread_id=str(row["id"]), run_id=str(row["invocation_id"])),
            )
            assert (await store.get(row["id"]))["status"] == "CANCELLED"
        report["cancel_wins_late_result"] = "passed"
        async with (
            await AsyncConnection.connect(conninfo(get_settings()), autocommit=True) as a,
            await AsyncConnection.connect(conninfo(get_settings()), autocommit=True) as b,
        ):
            key = uuid.uuid4().hex
            assert (
                await (
                    await a.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (key,))
                ).fetchone()
            )[0]
            assert not (
                await (
                    await b.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (key,))
                ).fetchone()
            )[0]
            await a.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))
            assert (
                await (
                    await b.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (key,))
                ).fetchone()
            )[0]
        report["checkpoint_session_lock"] = "passed"
        queued = []
        for _ in range(5):
            row = await store.create("customer-alice", "Admission test", uuid.uuid4().hex, "test")
            queued.append(row)
            created.append(row)
        try:
            await store.create("customer-alice", "Sixth pending run", uuid.uuid4().hex, "test")
        except HTTPException as exc:
            assert exc.status_code == 429
        else:
            raise AssertionError("unbounded_admission")
        report["bounded_customer_admission"] = "passed"
        for row in queued:
            await store.transition(row["id"], "customer-alice", row["revision"], "cancel")
        print(json.dumps(report))
    finally:
        # Leave an audit trail, clean only active synthetic runs from this probe.
        for row in created:
            current = await store.get(row["id"])
            if current["status"] == "QUEUED":
                await store.transition(row["id"], "customer-alice", current["revision"], "cancel")
        await resources.close()


if __name__ == "__main__":
    asyncio.run(main())
