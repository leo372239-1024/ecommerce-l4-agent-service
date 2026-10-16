import asyncio
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, ValidationError
from redis.exceptions import RedisError

from app.agent.contracts import StrictModel
from app.core.auth import Principal, digest, principal

router = APIRouter()


class SandboxRequest(StrictModel):
    tool: Literal["money_total", "csv_summary", "text_stats"]
    arguments: dict = Field(max_length=1)


class MoneyResult(StrictModel):
    total: str = Field(pattern=r"^\d{1,11}\.\d{2}$")
    count: int = Field(ge=1, le=200)
    business_effect: Literal["none"]


class TableResult(StrictModel):
    rows: int = Field(ge=1, le=200)
    columns: int = Field(ge=1, le=20)
    business_effect: Literal["none"]


class TextResult(StrictModel):
    characters: int = Field(ge=1, le=4000)
    lines: int = Field(ge=0, le=4000)
    words: int = Field(ge=0, le=4000)
    business_effect: Literal["none"]


async def invoke(socket_path, body):
    raw = json.dumps(body, ensure_ascii=False).encode() + b"\n"
    if len(raw) > 65536:
        raise HTTPException(413, "sandbox_request_limit")
    writer = None
    try:
        async with asyncio.timeout(3):
            reader, writer = await asyncio.open_unix_connection(socket_path, limit=8193)
            writer.write(raw)
            await writer.drain()
            response = await reader.readline()
            if len(response) > 8192 or not response.endswith(b"\n"):
                raise ValueError("invalid_result")
            value = json.loads(response)
            if value.get("ok") is not True:
                raise HTTPException(422, "sandbox_request_rejected")
            contract = {
                "money_total": MoneyResult,
                "csv_summary": TableResult,
                "text_stats": TextResult,
            }[body["tool"]]
            result = contract.model_validate(value["result"]).model_dump()
            return {
                "tool": body["tool"],
                "trust_class": "untrusted_computation",
                **result,
            }
    except (OSError, TimeoutError, ValueError, KeyError, ValidationError):
        raise HTTPException(503, "sandbox_unavailable") from None
    finally:
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


@router.post("/sandbox/execute")
async def execute(
    body: SandboxRequest, request: Request, actor: Annotated[Principal, Depends(principal)]
):
    settings = request.app.state.settings
    if not settings.sandbox_enabled:
        raise HTTPException(503, "sandbox_disabled")
    try:
        count = await request.app.state.resources.redis.eval(
            "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n",
            1,
            "sandbox:" + digest(actor.id),
        )
    except (RedisError, TimeoutError):
        raise HTTPException(503, "sandbox_admission_unavailable") from None
    if count > 30:
        raise HTTPException(429, "sandbox_rate_limit", headers={"Retry-After": "60"})
    return await invoke(settings.sandbox_socket_path, body.model_dump())
