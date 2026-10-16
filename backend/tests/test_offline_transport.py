"""Ensure the operational amendment cannot retry the agent's runtime model/tool requests."""

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "offline_transport", Path(__file__).resolve().parents[2] / "scripts/offline_prepare_retry.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "offline,host,statuses,expected",
    [
        (True, "dashscope.aliyuncs.com", [503, 200], 2),
        (False, "dashscope.aliyuncs.com", [503, 200], 1),
        (True, "openrouter.ai", [503, 200], 1),
        (True, "dashscope.aliyuncs.com", [401, 200], 1),
        (True, "dashscope.aliyuncs.com", [503, 503, 503, 200], 3),
    ],
)
async def test_only_offline_embedding_transport_changes_and_payload_stays_identical(
    monkeypatch, offline, host, statuses, expected
):
    sent = []

    def handler(request):
        sent.append((request.content, dict(request.extensions["timeout"])))
        return httpx.Response(statuses[len(sent) - 1], json={"ok": True})

    monkeypatch.setattr(module, "append_record", lambda _: None)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    token = module.OFFLINE.set(offline)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30) as client:
            request = client.build_request(
                "POST",
                f"https://{host}/embeddings",
                json={"input": ["same history"], "dimensions": 1024},
            )
            original = request.content
            result = await module.prepared_send(client, request)
            assert len(sent) == expected and result.status_code == statuses[expected - 1]
            assert all(payload == original for payload, _ in sent)
            assert request.extensions["timeout"]["read"] == 30
            assert all(
                options["read"] == (60 if offline and host == "dashscope.aliyuncs.com" else 30)
                for _, options in sent
            )
    finally:
        module.OFFLINE.reset(token)
