import re
from urllib.parse import urlsplit

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Link, Status

from app.core.config import Settings
from app.security.guards import redact

TECHNICAL = {
    "agent.workflow_id",
    "agent.invocation_id",
    "agent.node_id",
    "agent.tool_name",
    "agent.fencing",
    "agent.routing_effect",
    "http.method",
    "http.request.method",
    "http.status_code",
    "http.response.status_code",
    "exception.type",
}
STATIC_PATHS = {
    "/",
    "/api/v1/health/live",
    "/api/v1/health/ready",
    "/api/v1/metrics",
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    "/api/v1/auth/session",
    "/api/v1/auth/oidc/login",
    "/api/v1/auth/oidc/callback",
    "/api/v1/runs",
    "/api/v1/approvals",
    "/api/v1/cases",
    "/api/v1/knowledge/documents",
    "/api/v1/knowledge/upload",
    "/api/v1/knowledge/search",
    "/api/v1/memory",
    "/api/v1/memory/preferences",
    "/api/v1/memory/settings",
    "/api/v1/workflows",
    "/api/v1/workflows/candidates",
    "/api/v1/sandbox/execute",
}
DYNAMIC_PATH = re.compile(
    r"^/api/v1/(?:runs|approvals|cases|knowledge/(?:documents|evidence)|memory/items|workflows)/[a-fA-F0-9-]{36}(?:/(?:events|cancel|resume|approve|retry|claim|reconcile|close|review|export))?$"
)


def safe_path(value):
    try:
        path = urlsplit(str(value)).path
    except ValueError:
        return "/[unmatched]"
    return path if path in STATIC_PATHS or DYNAMIC_PATH.fullmatch(path) else "/[unmatched]"


def safe_attributes(attributes):
    result = {k: redact(v) for k, v in (attributes or {}).items() if k in TECHNICAL}
    for key in ("http.url", "url.full", "http.target", "url.path", "http.route"):
        if key in (attributes or {}):
            path = safe_path(attributes[key])
            result[key] = "http://service" + path if key in {"http.url", "url.full"} else path
    return result


def safe_span(span):
    # Export immutable sanitized copies. Raw URLs, headers, bodies and exception text never
    # reach the OTLP exporter; preserve context/parent and workflow identifiers for correlation.
    name = (
        span.name
        if span.name
        in {"agent.run", "agent.tool", "gateway.route", "gateway.mcp", "merchant.mcp.request"}
        else "http.request"
    )
    resource = Resource(
        {
            k: v
            for k, v in span.resource.attributes.items()
            if k
            in {
                "service.name",
                "service.version",
                "telemetry.sdk.name",
                "telemetry.sdk.version",
                "telemetry.sdk.language",
            }
        }
    )
    return ReadableSpan(
        name=name,
        context=span.context,
        parent=span.parent,
        kind=span.kind,
        resource=resource,
        attributes=safe_attributes(span.attributes),
        events=[
            Event(
                "exception" if e.name == "exception" else "technical.event",
                safe_attributes(e.attributes),
                e.timestamp,
            )
            for e in span.events
        ],
        links=[Link(link.context, safe_attributes(link.attributes)) for link in span.links],
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class SafeExporter(SpanExporter):
    def __init__(self, exporter):
        self.exporter = exporter

    def export(self, spans):
        return self.exporter.export([safe_span(span) for span in spans])

    def shutdown(self):
        self.exporter.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self.exporter.force_flush(timeout_millis)


def configure_telemetry(settings: Settings) -> TracerProvider | None:
    if not settings.otel_enabled:
        return None
    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            SafeExporter(
                OTLPSpanExporter(
                    endpoint=f"{settings.otel_exporter_otlp_endpoint.rstrip('/')}/v1/traces",
                    timeout=5,
                )
            )
        )
    )
    return provider
