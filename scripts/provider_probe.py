"""Explicit small real-provider probes. Output only sanitized capability metadata."""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.core.config import get_settings
from app.providers.dashscope import DashScopeClient, ProviderError


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["embedding", "rerank", "llm", "all"], default="all")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/step3/provider-probe.json")
    args = parser.parse_args()
    settings = get_settings()
    output = args.output
    report = (
        json.loads(output.read_text(encoding="utf-8"))
        if output.exists() and args.only != "all"
        else {}
    )
    report.update(
        {
            "embedding_model": settings.embedding_model,
            "reranker_model": settings.reranker_model,
            "llm_requested": settings.zhipu_model,
            "captured_at": datetime.now(UTC).isoformat(),
        }
    )
    async with httpx.AsyncClient(timeout=30) as client:
        adapter = DashScopeClient(settings, client)
        try:
            if args.only in ("embedding", "all"):
                vectors = await adapter.embed(["订单退款政策", "查询物流进度"])
                report["embedding"] = {
                    "status": "ok",
                    "count": len(vectors),
                    "dimensions": len(vectors[0]),
                }
        except ProviderError as error:
            report["embedding"] = {"status": "failed", "error": str(error)}
        try:
            if args.only in ("rerank", "all"):
                ranked = await adapter.rerank(
                    "如何查询退款进度", ["退款申请进度查询", "商品尺码说明", "物流运输路线"], 2
                )
                report["rerank"] = {"status": "ok", "indices": [x.index for x in ranked]}
        except ProviderError as error:
            report["rerank"] = {"status": "failed", "error": str(error)}
        key = settings.llm_api_key.get_secret_value()
        if args.only not in ("llm", "all"):
            pass
        elif not key:
            report["llm"] = {"status": "not_configured"}
        else:
            try:
                response = await client.post(
                    f"{settings.zhipu_api_base.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": settings.zhipu_model,
                        **settings.llm_request_options,
                        "temperature": settings.llm_temperature,
                        "max_tokens": 128,
                        "messages": [
                            {
                                "role": "user",
                                "content": "请用query_order工具查询订单TEST-001，不要猜测状态。",
                            }
                        ],
                        "tools": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "query_order",
                                    "description": "查询指定订单",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {"order_id": {"type": "string"}},
                                        "required": ["order_id"],
                                    },
                                },
                            }
                        ],
                    },
                )
                if response.is_success:
                    data = response.json()
                    calls = data["choices"][0]["message"].get("tool_calls", [])
                    valid = len(calls) == 1 and calls[0]["function"]["name"] == "query_order"
                    if valid:
                        valid = json.loads(calls[0]["function"]["arguments"]) == {
                            "order_id": "TEST-001"
                        }
                    report["llm"] = {
                        "status": "ok" if valid else "tool_call_not_confirmed",
                        "returned_model": data.get("model"),
                        "usage": data.get("usage"),
                    }
                else:
                    report["llm"] = {"status": "failed", "http_status": response.status_code}
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                report["llm"] = {"status": "failed", "error_type": type(error).__name__}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    checked = ("embedding", "rerank", "llm") if args.only == "all" else (args.only,)
    return int(any(report[name]["status"] != "ok" for name in checked))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
