"""One sanitized probe of the EXACT failed offline request; no benchmark rows changed."""

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import text

from app.context.conversation import ConversationService
from app.core.config import get_settings
from app.core.resources import Resources
from app.security.guards import safe_output


async def main(args):
    directory = Path("/artifacts/benchmarks") / args.experiment
    settings = get_settings()
    resource = Resources(settings)
    try:
        failure = json.loads(
            (directory / "offline-retry-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        )
        assert failure["status_code"] == 400, "diagnostic requires terminal HTTP400"
        target = failure["request_body_sha256"]
        async with resource.engine.connect() as conn:
            conversations = list(
                (
                    await conn.execute(
                        text(
                            "SELECT id FROM agent_conversations WHERE customer_id='customer-bob' AND deleted_at IS NULL AND expires_at>now() ORDER BY created_at DESC LIMIT 10"
                        )
                    )
                ).scalars()
            )
        payload = None
        for conversation_id in conversations:
            async with resource.engine.connect() as conn:
                historical = {
                    str(r["id"]): r["result"]
                    for r in (
                        await conn.execute(
                            text(
                                "SELECT id,result FROM agent_runs WHERE conversation_id=:id AND customer_id='customer-bob' ORDER BY conversation_seq"
                            ),
                            {"id": conversation_id},
                        )
                    ).mappings()
                }
            turns = await ConversationService(resource.engine).turns(
                "customer-bob", conversation_id
            )
            documents = [
                json.dumps({**t, "historical_result": historical[t["run_id"]]}, ensure_ascii=False)
                for t in turns
            ]
            for offset in range(0, len(documents), 10):
                candidate = {
                    "model": settings.embedding_model,
                    "input": documents[offset : offset + 10],
                    "dimensions": settings.embedding_dimensions,
                    "encoding_format": "float",
                }
                content = httpx.Request("POST", "https://example.invalid", json=candidate).content
                if hashlib.sha256(content).hexdigest() == target:
                    payload = candidate
                    break
            if payload is not None:
                break
        assert payload is not None, "failed request body not found; no probe sent"
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                settings.dashscope_embedding_api_base.rstrip("/") + "/embeddings",
                headers={
                    "Authorization": "Bearer " + settings.dashscope_api_key.get_secret_value()
                },
                json=payload,
            )
        body = response.json()
        error = body.get("error", body)
        report = {
            "captured_at": datetime.now(UTC).isoformat(),
            "scope": "single exact failed offline embedding request probe; no benchmark task/model/scoring mutations",
            "original_request_sha256": target,
            "request_body_match": True,
            "status_code": response.status_code,
            "input_lengths": [len(x) for x in payload["input"]],
            "usage": body.get("usage"),
            "embedding_count": len(body.get("data", [])),
            "error": safe_output(
                {k: error.get(k) for k in ("code", "type", "message")}
                if isinstance(error, dict) and not response.is_success
                else None
            ),
        }
        output = directory / args.output_name
        assert not output.exists(), "do not overwrite the first diagnostic"
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report))
    finally:
        await resource.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", default="step10-memory-confirmation-v1")
    parser.add_argument("--output-name", default="exact-failed-body-probe-298.json")
    args = parser.parse_args()
    assert Path(args.experiment).name == args.experiment
    assert Path(args.output_name).name == args.output_name
    asyncio.run(main(args))
