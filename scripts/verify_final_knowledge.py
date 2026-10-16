"""Final gateway RAG acceptance using disposable documents, without service interruption."""

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from verify_agent_core import ROOT, login, wait
from verify_context import checked, ready


async def main(args):
    alice, bob = await login("alice"), await login("bob")
    marker = "交付质保" + uuid.uuid4().hex[:8]
    document, version = None, 1
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "scope": "final deployed API, real gateway/embedding/reranker; disposable private document; acceptance only, not quality benchmark",
        "checks": [],
    }
    try:
        document = await checked(
            alice,
            "POST",
            "/knowledge/upload",
            status=202,
            files={
                "file": (
                    "delivery-acceptance.md",
                    (marker + "：演示商品质保一年，请保留购买凭证。").encode("utf-8"),
                    "text/markdown",
                )
            },
            data={"title": marker, "scope": "private"},
        )
        await ready(alice, document["id"], timeout=180)
        result = await checked(
            alice, "POST", "/knowledge/search", json={"query": marker + "的质保说明是什么"}
        )
        hit = next(item for item in result["items"] if item["document_id"] == document["id"])
        assert "质保一年" in hit["content"]
        assert result["diagnostics"]["retrieval"] == "hybrid_rrf"
        assert not result["diagnostics"]["reranker_degraded"]
        report["retrieval"] = result["diagnostics"]
        report["checks"].append("real_embedding_hybrid_retrieval_and_rerank")

        other = await checked(bob, "POST", "/knowledge/search", json={"query": marker})
        assert document["id"] not in {item["document_id"] for item in other["items"]}
        await checked(bob, "GET", "/knowledge/evidence/" + hit["chunk_id"], status=404)
        report["checks"].append("private_search_and_evidence_owner_isolation")

        run = await checked(
            alice,
            "POST",
            "/runs",
            status=202,
            headers={"Idempotency-Key": uuid.uuid4().hex},
            json={
                "message": f"查询知识库里{marker}的质保说明",
                "dataset_partition": "evaluation",
            },
        )
        run = await wait(alice, run["id"], 180)
        report["run"] = {
            "id": run["id"],
            "status": run["status"],
            "result": run.get("result"),
        }
        assert run["status"] == "SUCCEEDED", run["status"]
        answer = run["result"]["knowledge_answer"]
        assert answer["mode"] == "verified_extractive_rag" and answer["quotes"]
        by_id = {item["chunk_id"]: item for item in run["result"]["knowledge_citations"]}
        assert any(document["id"] == item["document_id"] for item in by_id.values())
        assert all(q["quote"] in by_id[q["chunk_id"]]["content"] for q in answer["quotes"])
        assert run["result"]["operation_refs"] == []
        assert run["result"]["usage"]["tool_calls"] == 0
        report["checks"].append("real_gateway_answer_literal_citations_no_business_side_effects")

        await checked(
            alice,
            "PUT",
            f"/knowledge/documents/{document['id']}",
            json={
                "expected_version": version,
                "document": {"title": marker, "text": marker + "：更新版本质保两年。"},
            },
        )
        version = 2
        await ready(alice, document["id"], timeout=180)
        await checked(alice, "GET", "/knowledge/evidence/" + hit["chunk_id"], status=404)
        current = await checked(alice, "POST", "/knowledge/search", json={"query": marker + "质保"})
        new = next(item for item in current["items"] if item["document_id"] == document["id"])
        assert new["version"] == 2 and "质保两年" in new["content"]
        report["checks"].append("replacement_invalidates_previous_evidence")
        report["status"] = "passed"
    finally:
        if document:
            await checked(
                alice,
                "DELETE",
                f"/knowledge/documents/{document['id']}",
                json={"expected_version": version},
            )
        await alice.aclose()
        await bob.aclose()
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "knowledge-verification.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({"status": report["status"], "checks": len(report["checks"])}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/current")
    asyncio.run(main(parser.parse_args()))
