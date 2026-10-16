"""Observe failed provider metadata around an unchanged frozen runner; no request mutation."""

import asyncio
import json
import os
import runpy
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import SecretStr

from app.core.config import get_settings

original_send = httpx.AsyncClient.send


def append_record(record):
    with Path(os.environ["HTTP_DIAG_OUTPUT"]).open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


async def observed_send(client, request, **kwargs):
    response = await original_send(client, request, **kwargs)
    if request.url.host == "dashscope.aliyuncs.com" and not response.is_success:
        await response.aread()
        settings = get_settings()
        try:
            body = response.json()
            error = body.get("error", {})
            payload = json.loads(request.content)
        except (ValueError, AttributeError):
            error, payload = {}, {}
        message = str(error.get("message", ""))
        for name in type(settings).model_fields:
            value = getattr(settings, name)
            if isinstance(value, SecretStr) and value.get_secret_value():
                message = message.replace(value.get_secret_value(), "[redacted]")
        inputs = payload.get("input", [])
        if isinstance(inputs, str):
            inputs = [inputs]
        record = {
            "captured_at": datetime.now(UTC).isoformat(),
            "endpoint": request.url.path,
            "status_code": response.status_code,
            "model": payload.get("model"),
            "dimensions": payload.get("dimensions"),
            "input_lengths": [len(x) for x in inputs if isinstance(x, str)],
            "error_code": error.get("code"),
            "error_type": error.get("type"),
            "message": message[:800],
            "scope": "pass-through observation only; requests and returned response unchanged",
        }
        await asyncio.to_thread(append_record, record)
    return response


if __name__ == "__main__":
    httpx.AsyncClient.send = observed_send
    sys.argv = sys.argv[1:]
    runpy.run_path(sys.argv[0], run_name="__main__")
