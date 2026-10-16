from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.metrics import MetricsMiddleware
from app.core.resources import Resources
from app.core.telemetry import configure_telemetry
from app.core.wakeups import EventWakeups
from app.security.middleware import SecurityHeaders, validation_error


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    provider = configure_telemetry(settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configure_logging(settings.log_level)
        resources = Resources(settings)
        application.state.resources = resources
        wakeups = EventWakeups(resources.redis)
        application.state.wakeups = wakeups
        wakeups.start()
        try:
            yield
        finally:
            await wakeups.close()
            await resources.close()
            if provider:
                provider.shutdown()

    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="电商 L4 智能客服 Agent API",
        lifespan=lifespan,
        telemetry={
            "tracer_provider": provider,
            "tracing": settings.otel_enabled,
            "metrics": False,
            "logs": False,
            "operation_spans": False,
        },
    )
    application.state.settings = settings
    application.add_exception_handler(RequestValidationError, validation_error)
    application.add_middleware(SecurityHeaders)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Last-Event-ID", "Idempotency-Key"],
    )
    application.add_middleware(MetricsMiddleware, application=application)
    application.add_middleware(
        RequestBodyLimitMiddleware, max_body_size=settings.upload_max_request_bytes
    )
    application.include_router(api_router, prefix="/api/v1")

    @application.get("/", tags=["meta"])
    async def root() -> dict[str, str]:
        return {
            "name": settings.app_name,
            "status": "ok",
            "version": "0.1.0",
            "capability": "controlled_merchant_agent"
            if settings.tool_routing_enabled
            else "read_only_agent_core",
        }

    return application


app = create_app()
