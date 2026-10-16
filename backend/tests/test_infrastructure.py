import io
import json
import logging

import httpx
import pytest
from ag_ui.core import EventType, RunStartedEvent
from ag_ui.encoder import EventEncoder
from fastapi import HTTPException, Request, UploadFile
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.core.logging import SafeJSONFormatter, redact_secrets
from app.core.resources import Resources
from app.core.uploads import iter_upload_chunks
from app.main import create_app
from app.providers.dashscope import DashScopeClient, ProviderError


def config(**kwargs):
    return Settings(_env_file=None, _secrets_dir=None, app_env="test", **kwargs)


def test_dimension_environment_is_parsed(monkeypatch):
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "1024")
    assert config().embedding_dimensions == 1024


def test_readiness_does_not_fake_dependency_success():
    app = create_app(config())
    with TestClient(app) as client:

        async def checks():
            return {"postgres_schema": "ok", "redis": "unavailable"}

        app.state.resources.checks = checks
        assert client.get("/api/v1/health/live").status_code == 200
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["redis"] == "unavailable"
        metrics = client.get("/api/v1/metrics").text
        assert 'route="/api/v1/health/ready",status="503"' in metrics


@pytest.mark.asyncio
async def test_connection_refused_is_dependency_failure(monkeypatch):
    resources = Resources(config())

    async def refused():
        raise ConnectionRefusedError("synthetic connection refused")

    async def available():
        return True

    monkeypatch.setattr(resources, "_postgres", refused)
    monkeypatch.setattr(resources.redis, "ping", available)
    try:
        assert await resources.checks() == {"postgres_schema": "unavailable", "redis": "ok"}
    finally:
        await resources.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_request_limit_including_without_content_length(streaming):
    app = create_app(config(upload_max_request_bytes=16))

    @app.post("/test-body")
    async def body(request: Request):
        return {"size": len(await request.body())}

    async def chunks():
        yield b"a" * 8
        yield b"b" * 9

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as c:
        assert (await c.post("/test-body", content=b"a" * 16)).status_code == 200
        over = await c.post("/test-body", content=chunks() if streaming else b"a" * 17)
        assert over.status_code == 413


@pytest.mark.asyncio
async def test_file_limit_known_and_unknown_size():
    for size in (None, 17):
        upload = UploadFile(io.BytesIO(b"x" * 17), size=size, filename="safe.txt")
        try:
            with pytest.raises(HTTPException) as raised:
                async for _ in iter_upload_chunks(upload, 16):
                    pass
            assert raised.value.status_code == 413
        finally:
            await upload.close()


@pytest.mark.asyncio
async def test_embedding_order_and_distinct_rerank_contract():
    def provider(request):
        data = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            assert data["dimensions"] == 64
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.2] * 64},
                        {"index": 0, "embedding": [0.1] * 64},
                    ]
                },
            )
        assert request.url.path.endswith("/reranks")
        assert "dimensions" not in data
        assert data["model"] == "qwen3-rerank"
        return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.9}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as c:
        adapter = DashScopeClient(
            config(dashscope_api_key=SecretStr("test-only"), embedding_dimensions=64), c
        )
        vectors = await adapter.embed(["订单", "物流"])
        assert vectors[0][0] == 0.1 and vectors[1][0] == 0.2
        assert (await adapter.rerank("物流", ["订单", "物流"], 1))[0].index == 1


@pytest.mark.asyncio
async def test_provider_failure_is_redacted():
    secret = "synthetic-secret-for-test"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(401, text=secret))
    ) as c:
        adapter = DashScopeClient(config(dashscope_api_key=SecretStr(secret)), c)
        with pytest.raises(ProviderError) as raised:
            await adapter.embed(["测试"])
        assert secret not in str(raised.value)
        assert "401" in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [[], [{"index": 0, "embedding": [0.1]}]])
async def test_wrong_embedding_dimension_or_count_rejected(rows):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": rows}))
    ) as c:
        adapter = DashScopeClient(config(dashscope_api_key=SecretStr("test-only")), c)
        with pytest.raises(ProviderError):
            await adapter.embed(["测试"])


def test_secrets_and_agui_contract():
    result = redact_secrets(None, None, {"authorization": "private", "nested": {"api_key": "x"}})
    assert result["authorization"] == "[REDACTED]"
    assert result["nested"]["api_key"] == "[REDACTED]"
    event = RunStartedEvent(type=EventType.RUN_STARTED, thread_id="t", run_id="r")
    encoded = EventEncoder().encode(event)
    assert "RUN_STARTED" in encoded and "runId" in encoded


def test_foreign_logger_and_exception_do_not_expose_provider_secret():
    secret = "sk-" + "a" * 32
    record = logging.LogRecord(
        "provider",
        logging.ERROR,
        "",
        0,
        "request failed %s",
        (secret,),
        (RuntimeError, RuntimeError("private provider response"), None),
    )
    output = SafeJSONFormatter().format(record)
    assert secret not in output and "private provider response" not in output
    assert json.loads(output)["error_type"] == "RuntimeError"
