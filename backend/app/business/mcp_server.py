"""Internal, fixed-publisher MCP endpoint. Provider credentials are never mounted here."""

import hmac
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as MCPToolError
from mcp.server.mcpserver.tools import Tool
from mcp.types import ToolAnnotations
from opentelemetry import trace
from opentelemetry.propagate import extract
from starlette.responses import JSONResponse

from app.agent.tools import ToolError
from app.business.catalog import CATALOG
from app.business.service import MerchantService
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.resources import Resources
from app.core.telemetry import configure_telemetry

execution_context = ContextVar("merchant_execution", default=None)
settings = get_settings()
resources = None


@asynccontextmanager
async def lifespan(_):
    global resources
    resources = Resources(settings)
    configure_logging(settings.log_level)
    provider = configure_telemetry(settings)
    if provider:
        trace.set_tracer_provider(provider)
    try:
        yield {}
    finally:
        await resources.close()
        if provider:
            provider.shutdown()


published_tools = []


def register(spec):
    # Separate signatures prevent model-supplied trusted context or write metadata on reads.
    async def read_tool(order_id: str) -> dict[str, Any]:
        context = execution_context.get()
        if not context:
            raise ValueError("execution_context_required")
        try:
            return await MerchantService(resources.engine).read(
                spec.id, {"order_id": order_id}, *context
            )
        except ToolError as exc:
            raise MCPToolError(exc.code) from None

    async def write_tool(order_id: str, operation_id: str, arg_hash: str) -> dict[str, Any]:
        context = execution_context.get()
        if not context:
            raise ValueError("execution_context_required")
        try:
            receipt = await MerchantService(resources.engine).write(
                spec.id,
                {"order_id": order_id, "operation_id": operation_id, "arg_hash": arg_hash},
                *context,
            )
        except ToolError as exc:
            raise MCPToolError(exc.code) from None
        if settings.app_env != "production":
            from sqlalchemy import text

            async with resources.engine.connect() as conn:
                payload = await conn.scalar(
                    text("SELECT payload FROM commerce_orders WHERE id=:id"), {"id": order_id}
                )
            delay = payload.get("simulator_write_response_delay_seconds", 0)
            if delay:
                import asyncio

                await asyncio.sleep(min(delay, 30))
        return receipt

    async def receipt_tool(order_id: str, operation_id: str | None = None) -> dict[str, Any]:
        context = execution_context.get()
        if not context:
            raise ValueError("execution_context_required")
        try:
            return await MerchantService(resources.engine).read(
                spec.id, {"order_id": order_id, "operation_id": operation_id}, *context
            )
        except ToolError as exc:
            raise MCPToolError(exc.code) from None

    tool = Tool.from_function(
        receipt_tool
        if spec.id == "get_operation_receipt"
        else (read_tool if spec.risk == "read" else write_tool),
        name=spec.id,
        description=spec.description,
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=spec.risk == "read",
            destructive_hint=spec.risk != "read",
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    tool.parameters = spec.schema
    # Replacing the advertised schema does not change the SDK-generated runtime
    # argument model. Its default ignores unknown keys. Enforce the same boundary
    # before the wrapper can discard model-supplied identity or metadata fields.
    tool.fn_metadata.arg_model.model_config.update(extra="forbid", strict=True)
    tool.fn_metadata.arg_model.model_rebuild(force=True)
    published_tools.append(tool)


for spec in CATALOG.values():
    register(spec)

server = MCPServer(
    "controlled-merchant-simulator", version="1.0.0", lifespan=lifespan, tools=published_tools
)

inner_app = server.streamable_http_app(
    stateless_http=True, json_response=True, host="0.0.0.0", max_request_body_size=65536
)


async def app(scope, receive, send):
    if scope["type"] != "http":
        await inner_app(scope, receive, send)
        return
    if scope["path"] == "/healthz" and scope["method"] == "GET":
        await JSONResponse({"status": "live"})(scope, receive, send)
        return
    headers = dict(scope["headers"])
    expected = settings.mcp_service_key.get_secret_value()
    supplied = headers.get(b"authorization", b"").decode("ascii", errors="ignore")
    if not expected or not hmac.compare_digest(supplied, "Bearer " + expected):
        await JSONResponse({"detail": "service_authentication_required"}, status_code=401)(
            scope, receive, send
        )
        return
    try:
        from uuid import UUID

        workflow = str(UUID(headers[b"x-workflow-id"].decode("ascii")))
        fencing = int(headers[b"x-execution-fence"])
        if fencing < 1:
            raise ValueError
    except (KeyError, ValueError, UnicodeError):
        await JSONResponse({"detail": "invalid_execution_context"}, status_code=400)(
            scope, receive, send
        )
        return
    token = execution_context.set((workflow, fencing))
    try:
        carrier = {
            key.decode("latin1"): value.decode("latin1")
            for key, value in headers.items()
            if key in {b"traceparent", b"tracestate"}
        }
        with trace.get_tracer("merchant-mcp").start_as_current_span(
            "merchant.mcp.request",
            context=extract(carrier),
            attributes={"agent.workflow_id": workflow, "agent.fencing": fencing},
        ):
            await inner_app(scope, receive, send)
    finally:
        execution_context.reset(token)
