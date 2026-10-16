import hashlib
import time
from typing import Annotated, Literal
from uuid import UUID

from ag_ui.encoder import EventEncoder
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import Field, field_validator
from redis.exceptions import RedisError
from sqlalchemy import text
from starlette.responses import StreamingResponse

from app.agent.contracts import ResumeInput, StrictModel
from app.agent.store import TERMINAL, RunStore
from app.core.auth import Principal, check_password, digest, identity, issue_session, principal
from app.security.guards import admit_message

router = APIRouter()
Owner = Annotated[str, Depends(identity)]


class Login(StrictModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class CreateRun(StrictModel):
    message: str = Field(min_length=1, max_length=4000)
    dataset_partition: Literal["development", "evaluation"] = "development"
    conversation_id: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )

    @field_validator("message")
    @classmethod
    def safe_message(cls, value):
        return admit_message(value)


class Revision(StrictModel):
    expected_revision: int = Field(ge=1)


class ResumeRun(Revision):
    input: ResumeInput


def public(row):
    return {
        key: row.get(key) if key.startswith("conversation_") else row[key]
        for key in (
            "id",
            "status",
            "revision",
            "generation",
            "invocation_id",
            "result",
            "event_seq",
            "created_at",
            "updated_at",
            "dataset_partition",
            "conversation_id",
            "conversation_seq",
        )
    }


@router.post("/auth/login")
async def login(body: Login, request: Request, response: Response):
    if request.app.state.settings.app_env == "production":
        raise HTTPException(503, "demo_login_disabled_in_production")
    # Shared Redis admission control bounds expensive password hashing across API replicas.
    try:
        count = await request.app.state.resources.redis.eval(
            "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n",
            1,
            "auth:login:" + digest(request.client.host if request.client else "unknown"),
        )
    except (RedisError, TimeoutError):
        raise HTTPException(503, "authentication_temporarily_unavailable") from None
    if count > 20:
        raise HTTPException(429, "login_rate_limit", headers={"Retry-After": "60"})
    async with request.app.state.resources.engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT * FROM customer_accounts WHERE username=:name"),
                    {"name": body.username},
                )
            )
            .mappings()
            .first()
        )
    # A fixed PBKDF2 workload also covers unknown usernames.
    encoded = row["password_hash"] if row else "pbkdf2_sha256$600000$dummy$" + "0" * 64
    if not await check_password(body.password, encoded) or not row or not row["active"]:
        raise HTTPException(401, "invalid_credentials")
    csrf = await issue_session(request, response, row["id"])
    return {"customer_id": row["id"], "role": row["role"], "csrf_token": csrf, "demo_only": True}


@router.post("/auth/logout", status_code=204)
async def logout(
    request: Request, response: Response, actor: Annotated[Principal, Depends(principal)]
):
    async with request.app.state.resources.engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM customer_sessions WHERE token_hash=:token"),
            {"token": digest(request.cookies["agent_session"])},
        )
    response.delete_cookie("agent_session", path="/")


@router.post("/runs", status_code=202)
async def create(
    body: CreateRun,
    request: Request,
    owner: Owner,
    idempotency_key: Annotated[
        str, Header(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    ],
):
    message = body.message.strip()
    if not message:
        raise HTTPException(422, "empty_message")
    if request.app.state.settings.tool_routing_enabled:
        from app.business.catalog import CATALOG
        from app.business.routing import indexed_metadata

        async with request.app.state.resources.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT tool_id,metadata,embedding IS NOT NULL AS ready FROM tool_catalog WHERE tool_id=ANY(:ids)"
                        ),
                        {"ids": list(CATALOG)},
                    )
                )
                .mappings()
                .all()
            )
        if len(rows) != len(CATALOG) or any(
            not row["ready"]
            or row["metadata"]
            != indexed_metadata(CATALOG[row["tool_id"]], request.app.state.settings)
            for row in rows
        ):
            raise HTTPException(503, "tool_catalog_not_ready")
    store = RunStore(request.app.state.resources.engine)
    row = await store.create(
        owner,
        message,
        idempotency_key,
        hashlib.sha256(
            (
                message
                + ("\nevaluation" if body.dataset_partition == "evaluation" else "")
                + ("\nconversation:" + body.conversation_id if body.conversation_id else "")
            ).encode()
        ).hexdigest(),
        body.dataset_partition,
        body.conversation_id,
    )
    return public(await store.get(row["id"], owner))


@router.get("/runs")
async def list_runs(request: Request, owner: Owner, limit: int = 20):
    if not 1 <= limit <= 100:
        raise HTTPException(422, "invalid_limit")
    async with request.app.state.resources.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT * FROM agent_runs WHERE customer_id=:owner ORDER BY created_at DESC LIMIT :limit"
                    ),
                    {"owner": owner, "limit": limit},
                )
            )
            .mappings()
            .all()
        )
    return {"items": [public(row) for row in rows]}


@router.get("/runs/{run_id}")
async def get_run(run_id: UUID, request: Request, owner: Owner):
    return public(await RunStore(request.app.state.resources.engine).get(run_id, owner))


@router.post("/runs/{run_id}/resume", status_code=202)
async def resume_run(run_id: UUID, body: ResumeRun, request: Request, owner: Owner):
    return public(
        await RunStore(request.app.state.resources.engine).transition(
            run_id, owner, body.expected_revision, "resume", body.input.model_dump(mode="json")
        )
    )


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: UUID, body: Revision, request: Request, owner: Owner):
    return public(
        await RunStore(request.app.state.resources.engine).transition(
            run_id, owner, body.expected_revision, "cancel"
        )
    )


@router.get("/runs/{run_id}/events")
async def events(
    run_id: UUID,
    request: Request,
    owner: Owner,
    after: int = 0,
    last_event_id: Annotated[str | None, Header()] = None,
):
    store = RunStore(request.app.state.resources.engine)
    row = await store.get(run_id, owner)
    try:
        cursor = int(last_event_id) if last_event_id is not None else after
        if not 0 <= cursor <= row["event_seq"]:
            raise ValueError("invalid_cursor")
    except ValueError:
        raise HTTPException(409, "invalid_event_cursor") from None

    async def stream():
        nonlocal cursor
        # Resolve each payload through the installed protocol models, not handcrafted wire aliases.
        from ag_ui.core import Event
        from pydantic import TypeAdapter

        adapter, encoder = TypeAdapter(Event), EventEncoder()
        deadline = time.monotonic() + 25
        try:
            while time.monotonic() < deadline and not await request.is_disconnected():
                # Sessions can be revoked while a stream is open.
                await identity(request)
                batch = await store.events(run_id, cursor)
                for event in batch:
                    cursor = event["seq"]
                    yield f"id: {cursor}\n" + encoder.encode(
                        adapter.validate_python(event["payload"])
                    )
                current = await store.get(run_id, owner)
                if not batch and (
                    current["status"] in TERMINAL
                    or current["status"] in {"WAITING_USER", "WAITING_APPROVAL"}
                ):
                    break
                if not batch:
                    yield ": heartbeat\n\n"
                    await request.app.state.wakeups.wait(str(run_id), timeout=1)
        except HTTPException:
            # Expired/revoked auth closes the stream; reconnect requires authentication.
            return

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
