"""Read back browser acceptance outcomes and verify same-origin authorization/replay contracts."""

import argparse
import asyncio
import json
from datetime import UTC, datetime

import httpx
from verify_agent_core import ROOT, login
from verify_business_flows import observe


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="artifacts/step8")
    args = parser.parse_args()
    out = ROOT / args.output_dir
    browser = json.loads((out / "browser-acceptance.json").read_text(encoding="utf-8"))
    fixtures = json.loads((out / "browser-fixtures.json").read_text(encoding="utf-8"))
    customer, other, support, supervisor = [
        await login(name) for name in ("alice", "bob", "support", "supervisor")
    ]
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "checks": [],
        "browser_checks": len(browser["checks"]),
        "quality_benchmark": False,
    }

    def passed(name):
        report["checks"].append(name)
        print(json.dumps({"passed": name}), flush=True)

    async def get(client, path, status=200):
        response = await client.get(path)
        assert response.status_code == status, (path, response.status_code)
        return response.json() if response.content else None

    try:
        async with httpx.AsyncClient(base_url="http://localhost:8080/api/v1") as anonymous:
            config = await get(anonymous, "/auth/config")
            assert set(config) == {"demo_login_enabled", "oidc_enabled"}
            await get(anonymous, "/runs", 401)
        passed("public_auth_config_and_anonymous_guard")
        for client, role in (
            (customer, "customer"),
            (support, "support"),
            (supervisor, "supervisor"),
        ):
            assert (await get(client, "/auth/session"))["role"] == role
        passed("server_resolved_role_and_session")
        memories = await get(customer, "/memories")
        assert type(memories["automatic_memory_enabled"]) is bool
        await get(support, "/memories", 403)
        denied = await customer.put(
            "/memories/preferences",
            headers={"X-CSRF-Token": "invalid"},
            json={"key": "language", "value": "en"},
        )
        assert denied.status_code == 403
        passed("memory_settings_role_and_csrf_guard")
        documents = (await get(customer, "/knowledge/documents"))["items"]
        marker = fixtures["return"].rsplit("-", 1)[-1]
        private = next(d for d in documents if d["title"] == "步骤8质保验收" + marker)
        assert private["can_manage"] and private["visible_roles"]
        other_docs = (await get(other, "/knowledge/documents"))["items"]
        assert private["id"] not in {d["id"] for d in other_docs}
        staff_docs = (await get(support, "/knowledge/documents"))["items"]
        policy = next(d for d in staff_docs if d["title"] == "步骤8共享政策" + marker)
        assert policy["validation"] == "APPROVED" and not policy["can_manage"]
        passed("document_management_capability_and_private_isolation")
        evidence = next(
            c
            for c in browser["checks"]
            if c["name"] == "knowledge_create_upload_ingest_search_evidence"
        )
        await get(other, "/knowledge/evidence/" + evidence["chunk_id"], 404)
        passed("evidence_reauthorizes_reader")
        query = next(c for c in browser["checks"] if c["name"] == "task_submit_and_missing_order")
        run_id = query["run_id"]
        run = await get(customer, "/runs/" + run_id)
        assert run["status"] == "SUCCEEDED"
        await get(other, "/runs/" + run_id, 404)
        await get(support, "/runs/" + run_id, 403)
        response = await customer.get(f"/runs/{run_id}/events", headers={"Last-Event-ID": "20"})
        response.raise_for_status()
        ids = [int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")]
        assert ids == list(range(21, run["event_seq"] + 1))
        tail = await customer.get(
            f"/runs/{run_id}/events", headers={"Last-Event-ID": str(run["event_seq"])}
        )
        assert tail.status_code == 200 and "data:" not in tail.text
        await get(customer, f"/runs/{run_id}/events?after={run['event_seq'] + 1}", 409)
        passed("task_isolation_and_exact_sse_replay")
        before = json.loads((out / "before-supervisor.json").read_text(encoding="utf-8"))
        assert before["operation_count"] == 0 and before["refunded_minor"] == 0
        after = observe(fixtures["refund"])
        assert after["operation_count"] == 1 and after["refunded_minor"] == 249900
        refund = next(c for c in browser["checks"] if c["name"] == "supervisor_refund_receipt")
        receipt = await get(customer, "/operations/" + refund["operation_id"])
        assert (
            receipt["status"] == "SUCCEEDED" and receipt["receipt"]["fields"]["simulated_business"]
        )
        denied = await customer.post(
            "/approvals/" + refund["operation_id"] + "/decision",
            json={"expected_revision": 1, "decision": "confirm"},
        )
        assert denied.status_code == 409 and observe(fixtures["refund"])["operation_count"] == 1
        rejection = next(
            c
            for c in browser["checks"]
            if c["name"] == "customer_rejects_refund_without_business_write"
        )
        assert (await get(customer, "/runs/" + rejection["run_id"]))["status"] == "CANCELLED"
        rejected = observe(fixtures["reject"])
        assert rejected["operation_count"] == 0 and rejected["refunded_minor"] == 0
        passed("no_write_before_supervisor_single_receipt_and_stale_decision_guard")
        handoff = next(c for c in browser["checks"] if c["name"] == "support_claim_reconcile_close")
        case = await get(support, "/cases/" + handoff["case_id"])
        assert case["status"] == "CLOSED" and case["assigned_to"] == "staff-support"
        assert "operation_receipts" in case
        assert (await get(customer, "/cases/" + handoff["case_id"]))["resolution"] == case[
            "resolution"
        ]
        await get(other, "/cases/" + handoff["case_id"], 404)
        passed("case_receipts_authorization_and_customer_resolution")
        workflow = next(
            c
            for c in browser["checks"]
            if c["name"] == "awm_candidate_validate_approve_skill_download"
        )
        templates = (await get(customer, "/workflows"))["items"]
        assert (
            next(w for w in templates if w["id"] == workflow["workflow_id"])["status"] == "DISABLED"
        )
        await get(customer, "/workflows/" + workflow["workflow_id"] + "/skill", 404)
        passed("disabled_awm_cannot_be_exported")
    finally:
        for client in (customer, other, support, supervisor):
            await client.aclose()
    (out / "workbench-contract-verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"{len(report['checks'])} workbench contract checks passed")


if __name__ == "__main__":
    asyncio.run(main())
