"""Runs inside a Docker container against a dedicated evaluation database."""

import argparse
import asyncio
import hashlib
import json
import random
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.agent.store import RunStore
from app.api.routes.approvals import Decision, decide
from app.business.catalog import CATALOG, canonical_hash
from app.business.fixtures import details
from app.business.routing import index_catalog
from app.core.auth import Principal
from app.core.config import get_settings
from app.core.resources import Resources
from app.evaluation.oracle import compare, grade, pass_k, wilson
from app.evaluation.simulator import scripted_decision
from app.evaluation.variants import VARIANTS
from app.worker import execute_run, publish_outbox

PROVIDER_CALLS = ContextVar("benchmark_provider_calls", default=None)
ORIGINAL_SEND = httpx.AsyncClient.send


async def measured_send(self, request, **kwargs):
    calls = PROVIDER_CALLS.get()
    started = time.monotonic()
    try:
        response = await ORIGINAL_SEND(self, request, **kwargs)
    except httpx.HTTPError as exc:
        if calls is not None and request.url.host in {
            "open.bigmodel.cn",
            "api.deepseek.com",
            "openrouter.ai",
            "dashscope.aliyuncs.com",
        }:
            calls.append(
                {
                    "provider": request.url.host,
                    "endpoint": request.url.path,
                    "status_code": 0,
                    "error_type": type(exc).__name__,
                    "model": None,
                    "usage": None,
                    "elapsed_seconds": time.monotonic() - started,
                }
            )
        raise
    if calls is not None and request.url.host in {
        "open.bigmodel.cn",
        "api.deepseek.com",
        "openrouter.ai",
        "dashscope.aliyuncs.com",
    }:
        await response.aread()
        try:
            body = response.json()
        except ValueError:
            body = {}
        calls.append(
            {
                "provider": request.url.host,
                "endpoint": request.url.path,
                "status_code": response.status_code,
                "model": body.get("model"),
                "serving_provider": body.get("provider"),
                "usage": body.get("usage"),
                "elapsed_seconds": time.monotonic() - started,
            }
        )
    return response


async def snapshot(engine, aliases):
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("SELECT * FROM commerce_orders WHERE id=ANY(:ids)"),
                    {"ids": list(aliases.values())},
                )
            )
            .mappings()
            .all()
        )
    return {r["id"]: dict(r) for r in rows}


async def episode(settings, resources, fixture_engine, case, variant, trial, output):
    fixture_started = time.monotonic()
    provider_calls = []
    PROVIDER_CALLS.set(provider_calls)
    prefix = "EV-" + uuid.uuid4().hex[:14]
    aliases = {a: prefix + "-" + a for a in case["fixture"]["orders"]}
    expected = {
        aliases[a]: {
            "id": aliases[a],
            "customer_id": o["owner"],
            "version": 1,
            "payload": {**o["payload"], "details": details(aliases[a], o["payload"]["paid_minor"])},
        }
        for a, o in case["fixture"]["orders"].items()
    }
    fixture_retries = 0
    for attempt in range(3):
        try:
            async with fixture_engine.begin() as conn:
                for row in expected.values():
                    await conn.execute(
                        text(
                            "INSERT INTO commerce_orders VALUES(:id,:owner,1,CAST(:p AS jsonb)) ON CONFLICT (id) DO NOTHING"
                        ),
                        {
                            "id": row["id"],
                            "owner": row["customer_id"],
                            "p": json.dumps(row["payload"], ensure_ascii=False),
                        },
                    )
            before = await snapshot(fixture_engine, aliases)
            if before != expected:
                raise RuntimeError("fixture_state_mismatch")
            break
        except TimeoutError:
            if attempt == 2:
                raise
            fixture_retries += 1
            await asyncio.sleep(1 + attempt)
    fixture_seconds = time.monotonic() - fixture_started
    started = time.monotonic()
    store = RunStore(resources.engine)
    message = case["message"].format(**aliases)
    conversation_id = None
    owner = case.get("customer_id", "customer-alice")
    history_seed_started = time.monotonic()
    if case.get("history_fixture"):
        from app.context.conversation import ConversationService
        from app.evaluation.specialists import seed_history

        conversation_id = (await ConversationService(fixture_engine).create(owner))["id"]
        await seed_history(fixture_engine, owner, conversation_id, case, aliases)
        if variant == "M-vector":
            from app.evaluation.specialists import index_history

            await index_history(settings, fixture_engine, owner, conversation_id)
    history_seed_seconds = time.monotonic() - history_seed_started
    started = time.monotonic()
    controller = None
    harness_class = VARIANTS[variant]
    if case.get("fault"):
        from app.evaluation.specialists import FaultController

        controller = FaultController(fixture_engine, case["fault"], aliases)
        harness_class = controller.wrap(harness_class)
    row = await store.create(
        owner,
        message,
        uuid.uuid4().hex,
        hashlib.sha256(message.encode()).hexdigest(),
        "evaluation",
        conversation_id=conversation_id,
    )
    run_id = str(row["id"])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(resources=resources)))
    for _ in range(12):
        claimed = await store.claim("benchmark-isolated", 120, run_id)
        if not claimed or str(claimed["id"]) != run_id:
            raise RuntimeError("evaluation_queue_not_isolated")
        await execute_run(settings, resources, claimed, harness_class)
        row = await store.get(run_id)
        if row["status"] == "WAITING_APPROVAL":
            proposal = row["result"]["proposal"]
            operation_id = uuid.UUID(proposal["operation_id"])
            # Scripted users confirm only their explicitly requested intent, never
            # an Agent-added write in a read-only or unsupported request. This uses
            # the allowed dialogue script, not the hidden terminal oracle.
            decision = await decide(
                operation_id,
                Decision(
                    expected_revision=proposal["revision"],
                    decision=scripted_decision(case, proposal, aliases),
                ),
                request,
                Principal(owner, "customer", "demo-shop"),
            )
            if decision["status"] == "CONFIRMED":
                await decide(
                    operation_id,
                    Decision(expected_revision=decision["revision"], decision="approve"),
                    request,
                    Principal("staff-supervisor", "supervisor", "demo-shop"),
                )
        elif row["status"] == "WAITING_USER":
            if case["scripted_user"].get("missing_order") is None:
                break
            await store.transition(
                run_id,
                owner,
                row["revision"],
                "resume",
                {"order_ids": [aliases[case["scripted_user"]["missing_order"]]]},
            )
        else:
            break
    else:
        raise RuntimeError("simulator_turn_budget")
    after = await snapshot(fixture_engine, aliases)
    if controller:
        # Exogenous simulator changes are expected by BOTH groups. Scoring still verifies
        # model goals/evidence/effects independently; these are not Agent writes.
        for change in controller.events:
            kind = change.get("fault")
            if kind in {"amount_changed", "logistics_changed", "eligibility_changed"}:
                field, value = {
                    "amount_changed": ("paid_minor", 15800),
                    "logistics_changed": ("logistics", "运输异常已更新"),
                    "eligibility_changed": ("delivered_days", 30),
                }[kind]
                before[aliases["A"]]["payload"][field] = value
                before[aliases["A"]]["version"] = 2
            elif kind == "permanent_unavailable":
                before[aliases["A"]]["payload"]["simulator_transient_error"] = True
    async with fixture_engine.connect() as conn:
        operations = [
            dict(r)
            for r in (
                await conn.execute(
                    text(
                        "SELECT b.*,p.order_id,p.action FROM business_operations b JOIN business_proposals p USING(operation_id) WHERE p.workflow_id=:id"
                    ),
                    {"id": run_id},
                )
            ).mappings()
        ]
        proposals = [
            dict(r)
            for r in (
                await conn.execute(
                    text("SELECT * FROM business_proposals WHERE workflow_id=:id"), {"id": run_id}
                )
            ).mappings()
        ]
        events = [
            dict(r)
            for r in (
                await conn.execute(
                    text("SELECT seq,payload FROM agent_events WHERE workflow_id=:id ORDER BY seq"),
                    {"id": run_id},
                )
            ).mappings()
        ]
    score = grade(case, row, aliases, before, after, operations, proposals)
    chat_calls = [
        c
        for c in provider_calls
        if c["provider"] in {"open.bigmodel.cn", "api.deepseek.com", "openrouter.ai"}
    ]
    usage = {
        "llm_calls": len(chat_calls),
        "total_tokens": sum((c["usage"] or {}).get("total_tokens", 0) for c in chat_calls),
        "prompt_tokens": sum((c["usage"] or {}).get("prompt_tokens", 0) for c in chat_calls),
        "completion_tokens": sum(
            (c["usage"] or {}).get("completion_tokens", 0) for c in chat_calls
        ),
        "failed_requests": sum(
            c["status_code"] == 0 or c["status_code"] >= 400 for c in provider_calls
        ),
        "retrieval_calls": len(provider_calls) - len(chat_calls),
        "usage_missing_calls": sum(c["usage"] is None for c in provider_calls),
    }
    result = {
        "case_id": case["id"],
        "family": case["family"],
        "difficulty": case["difficulty"],
        "variant": variant,
        "trial": trial,
        "workflow_id": run_id,
        "status": row["status"],
        "elapsed_seconds": round(time.monotonic() - started, 4),
        "fixture_seconds": round(fixture_seconds, 4),
        "fixture_retries": fixture_retries,
        "history_seed_and_offline_index_seconds": history_seed_seconds,
        "usage": usage,
        "fault_events": controller.events if controller else [],
        **score,
    }
    with (output / "case-results.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(result, ensure_ascii=False) + "\n")
    with (output / "trajectory.jsonl").open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(
                {
                    "workflow_id": run_id,
                    "case_id": case["id"],
                    "variant": variant,
                    "trial": trial,
                    "before": before,
                    "after": after,
                    "events": events,
                },
                ensure_ascii=False,
                default=str,
            )
            + "\n"
        )
    with (output / "provider-calls.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps({"workflow_id": run_id, "calls": provider_calls}) + "\n")
    if conversation_id:
        await (
            __import__("app.context.conversation", fromlist=["ConversationService"])
            .ConversationService(fixture_engine)
            .delete(owner, conversation_id)
        )
    if row["status"] == "WAITING_USER":
        # Preserve scored snapshot, release admission without supplying a hidden oracle answer.
        await store.transition(run_id, owner, row["revision"], "cancel")
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "case_id",
                    "variant",
                    "trial",
                    "status",
                    "strict_success",
                    "elapsed_seconds",
                )
            }
        ),
        flush=True,
    )
    return result


async def main(args):
    httpx.AsyncClient.send = measured_send
    benchmark_started = time.monotonic()
    settings = get_settings()
    if settings.postgres_db != "agent_benchmark_step9":
        raise RuntimeError("dedicated_benchmark_database_required")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    selected = dataset["cases"][: args.limit] if args.limit else dataset["cases"]
    if args.case_ids:
        selected = [c for c in selected if c["id"] in args.case_ids.split(",")]
    resources = Resources(settings)
    # Controller-only initialization/inspection, not the Agent's database budget.
    fixture_engine = create_async_engine(
        settings.database_url,
        pool_size=4,
        max_overflow=0,
        pool_pre_ping=True,
        pool_timeout=30,
        connect_args={"timeout": 10, "command_timeout": 30},
    )
    stop = asyncio.Event()

    async def relay():
        from app.business.service import MerchantService

        while not stop.is_set():
            await publish_outbox(resources)
            await MerchantService(resources.engine).consume_events()
            await asyncio.sleep(1)

    relay_task = asyncio.create_task(relay())
    try:
        await index_catalog(settings, resources.engine)
        async with resources.engine.connect() as conn:
            identities = dict(
                (await conn.execute(text("SELECT id,tenant_id FROM customer_accounts"))).all()
            )
            if identities.get("customer-alice") != "demo-shop":
                raise RuntimeError("unexpected_benchmark_tenant")
            policy_snapshot = dict(
                (
                    await conn.execute(
                        text("SELECT * FROM merchant_policies WHERE tenant_id='demo-shop'")
                    )
                )
                .mappings()
                .one()
            )
            if policy_snapshot["supervisor_threshold_minor"] != 5000:
                raise RuntimeError("frozen_policy_mismatch")
            context_counts = {
                table: await conn.scalar(text("SELECT count(*) FROM " + table))
                for table in ("knowledge_chunks", "customer_memories", "workflow_memories")
            }
        schedule = [
            (case, variant, trial)
            for trial in range(args.trials)
            for case in selected
            for variant in args.variants.split(",")
        ]
        random.Random(args.seed).shuffle(schedule)
        manifest = {
            "captured_at": datetime.now(UTC).isoformat(),
            "dataset_sha256": hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest(),
            "variants": args.variants.split(","),
            "trials": args.trials,
            "seed": args.seed,
            "cases": len(selected),
            "schedule": [{"case_id": c["id"], "variant": v, "trial": t} for c, v, t in schedule],
            "model": settings.zhipu_model,
            "llm_request_options": settings.llm_request_options,
            "llm_temperature": settings.llm_temperature,
            "dataset_filename": Path(args.dataset).name,
            "llm_api_base": settings.zhipu_api_base,
            "temperature": settings.llm_temperature,
            "concurrency": args.concurrency,
            "soak_seconds": args.soak_seconds,
            "soak_interval_seconds": args.soak_interval_seconds,
            "catalog_sha256": canonical_hash([s.metadata() for s in CATALOG.values()]),
            "catalog_tools": len(CATALOG),
            "policy_snapshot": policy_snapshot,
            "policy_sha256": canonical_hash(policy_snapshot),
            "context_snapshot_counts": context_counts,
            "embedding_model": settings.embedding_model,
            "reranker_model": settings.reranker_model,
            "provider_revision_note": "Requested names and returned model/usage are archived. Provider immutable revision not available; repetitions need not be bitwise deterministic.",
            "business": "isolated_simulated_merchant",
            "human_review": dataset["review_status"],
            "comparison_status": "exploratory",
            "cached_inference": False,
            "provider_prefix_cache": "Vendor managed; reported cache hit/miss tokens archived in raw usage. No response replay by evaluator.",
            "limits": {
                "llm_calls": 12,
                "tokens": 20000,
                "tools": 30,
                "active_seconds": settings.run_active_seconds,
            },
            "ablation_limitations": {
                "A-plan": "Model-generated linear sequence replaces compiled DAG; same intent, routing, context, verifier, reflection and gateway. No overall DAG validation.",
                "A-memory": "context packet removal only; evaluation disables historical memory/AWM so no memory benefit inference",
                "A-route": "dense_top6 replacing hybrid+rerank, common gateway unchanged",
                "A-reflect": "one transient read retry, no external-feedback repair",
            },
        }
        manifest_path = output / "manifest.json"
        if manifest_path.exists():
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if any(
                previous[key] != manifest[key]
                for key in (
                    "dataset_sha256",
                    "variants",
                    "trials",
                    "seed",
                    "schedule",
                    "model",
                    "llm_api_base",
                    "llm_request_options",
                    "limits",
                    "concurrency",
                    "soak_seconds",
                    "soak_interval_seconds",
                    "policy_sha256",
                    "catalog_sha256",
                )
            ):
                raise RuntimeError("experiment_parameters_changed")
        existing = []
        if (output / "case-results.jsonl").exists():
            existing = [
                json.loads(line)
                for line in (output / "case-results.jsonl").read_text(encoding="utf-8").splitlines()
            ]
        done = {(r["case_id"], r["variant"], r["trial"]) for r in existing}
        rows = list(existing)
        (output / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        semaphore = asyncio.Semaphore(args.concurrency)

        async def scheduled(case, variant, trial):
            if (case["id"], variant, trial) in done:
                return
            async with semaphore:
                try:
                    rows.append(
                        await episode(
                            settings, resources, fixture_engine, case, variant, trial, output
                        )
                    )
                except Exception as exc:
                    # Stop the batch on simulator failure; don't silently exclude any result.
                    (output / "interruption.json").write_text(
                        json.dumps(
                            {
                                "case_id": case["id"],
                                "variant": variant,
                                "trial": trial,
                                "error_type": type(exc).__name__,
                                "error": str(exc)[:250],
                            }
                        ),
                        encoding="utf-8",
                    )
                    raise

        await asyncio.gather(*(scheduled(c, v, t) for c, v, t in schedule))
        # Extend a short schedule with real read tasks in a separate archive. Repeated
        # soak tasks never increase the quality experiment's sample size.
        soak_output = output / "soak"
        soak_output.mkdir(exist_ok=True)
        soak_count = 0
        while time.monotonic() - benchmark_started < args.soak_seconds:
            arrival_started = time.monotonic()
            await episode(
                settings,
                resources,
                fixture_engine,
                dataset["cases"][soak_count % 4],
                "P",
                10000 + soak_count,
                soak_output,
            )
            soak_count += 1
            # A steady real-task arrival rate keeps soak cost bounded. Never extend
            # with an empty timer after the last task; next real arrivals continue.
            remaining = args.soak_interval_seconds - (time.monotonic() - arrival_started)
            if (
                remaining > 0
                and time.monotonic() - benchmark_started + remaining < args.soak_seconds
            ):
                await asyncio.sleep(remaining)
        (output / "soak-workload.json").write_text(
            json.dumps(
                {
                    "elapsed_seconds": time.monotonic() - benchmark_started,
                    "target_seconds": args.soak_seconds,
                    "additional_real_model_read_tasks": soak_count,
                    "included_in_quality_scores": False,
                    "arrival_interval_seconds": args.soak_interval_seconds,
                    "workload": "Real model read arrivals, paced after the quality schedule; not a saturation test or an idle timer.",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        summary = summarize_results(rows)
        (output / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False))
    finally:
        stop.set()
        relay_task.cancel()
        await asyncio.gather(relay_task, return_exceptions=True)
        await resources.close()
        await fixture_engine.dispose()


def summarize_results(rows):
    summary = {"variants": {}, "comparisons": {}}
    for variant in sorted({r["variant"] for r in rows}):
        items = [r for r in rows if r["variant"] == variant]
        successes = sum(r["strict_success"] for r in items)
        first_trial = min(r["trial"] for r in items)
        first = [r for r in items if r["trial"] == first_trial]
        latencies = sorted(r["elapsed_seconds"] for r in items)
        by_case = {}
        for r in items:
            by_case.setdefault(r["case_id"], []).append(r["strict_success"])
        summary["variants"][variant] = {
            "episodes": len(items),
            "cases": len(by_case),
            "successes": successes,
            "success_rate": successes / len(items),
            "first_trial_wilson95": wilson(sum(r["strict_success"] for r in first), len(first)),
            "safety_failures": sum(bool(r["safety_failures"]) for r in items),
            "p50_seconds": latencies[len(latencies) // 2],
            "p95_seconds": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))],
            "tokens": sum(r["usage"].get("total_tokens", 0) for r in items),
            "model_calls": sum(r["usage"].get("llm_calls", 0) for r in items),
            "pass5": sum(pass_k(sum(v), len(v), 5) for v in by_case.values()) / len(by_case)
            if all(len(v) >= 5 for v in by_case.values())
            else None,
        }
        if variant != "P":
            summary["comparisons"][variant] = compare(rows, variant, "P")
    summary["cost_currency"] = None
    summary["cost_reason"] = (
        "No verified billing schedule/invoice; tokens are reported, monetary cost unmeasured. Missing retrieval usage is identified per request in provider-calls.jsonl."
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="/evaluation/step9-dataset.json")
    parser.add_argument("--output", default="/artifacts/benchmarks/step9-main")
    parser.add_argument("--variants", default="B0,B1,P,A-plan,A-route,A-memory,A-reflect")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--seed", type=int, default=901)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case-ids")
    parser.add_argument("--concurrency", type=int, default=2, choices=[1, 2, 4])
    parser.add_argument("--soak-seconds", type=int, default=0)
    parser.add_argument("--soak-interval-seconds", type=float, default=30)
    asyncio.run(main(parser.parse_args()))
