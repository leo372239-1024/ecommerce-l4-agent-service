"""Audited transport amendment for offline history vectors; frozen agent execution unchanged."""

import asyncio
import hashlib
import json
import os
import runpy
import sys
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

import httpx

ORIGINAL_SEND = httpx.AsyncClient.send
OFFLINE = ContextVar("offline_history_preparation", default=False)


def append_record(record):
    with Path(os.environ["OFFLINE_RETRY_LOG"]).open("a", encoding="utf-8") as file:
        file.write(json.dumps(record) + "\n")


async def prepared_send(client, request, **kwargs):
    if not (
        OFFLINE.get()
        and request.url.host == "dashscope.aliyuncs.com"
        and request.url.path.endswith("/embeddings")
    ):
        return await ORIGINAL_SEND(client, request, **kwargs)
    old_timeout = request.extensions.get("timeout")
    request.extensions["timeout"] = {**(old_timeout or {}), "read": 60.0}
    digest = hashlib.sha256(request.content).hexdigest()
    try:
        for attempt in range(1, 4):
            started = time.monotonic()
            error = None
            response = None
            try:
                response = await ORIGINAL_SEND(client, request, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                error = exc
            status = response.status_code if response is not None else 0
            retry = attempt < 3 and (error is not None or status in {408, 429, 500, 502, 503, 504})
            if retry or error is not None or status >= 400:
                await asyncio.to_thread(
                    append_record,
                    {
                        "captured_at": datetime.now(UTC).isoformat(),
                        "phase": "offline_history_preparation_only",
                        "attempt": attempt,
                        "request_body_sha256": digest,
                        "provider": request.url.host,
                        "endpoint": request.url.path,
                        "status_code": status,
                        "error_type": type(error).__name__ if error else None,
                        "elapsed_seconds": time.monotonic() - started,
                        "disposition": "retry" if retry else "terminal_return_to_original_runner",
                        "usage": None,
                        "usage_scope": "unreported failed call; separate from original runner's final-attempt metadata",
                    },
                )
            if not retry:
                if error:
                    raise error
                return response
            if response is not None:
                await response.aclose()
            await asyncio.sleep(2 ** (attempt - 1))
    finally:
        if old_timeout is None:
            request.extensions.pop("timeout", None)
        else:
            request.extensions["timeout"] = old_timeout


def install():
    from app.evaluation import specialists

    original_index = specialists.index_history

    async def offline_index(*args, **kwargs):
        token = OFFLINE.set(True)
        try:
            return await original_index(*args, **kwargs)
        finally:
            OFFLINE.reset(token)

    specialists.index_history = offline_index
    httpx.AsyncClient.send = prepared_send


if __name__ == "__main__":
    install()
    sys.argv = sys.argv[1:]
    runpy.run_path(sys.argv[0], run_name="__main__")
