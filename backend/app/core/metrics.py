import time

from prometheus_client import Counter, Histogram
from starlette.routing import compile_path

REQUESTS = Counter("agent_http_requests_total", "HTTP responses", ["method", "route", "status"])
LATENCY = Histogram("agent_http_request_duration_seconds", "HTTP latency", ["method", "route"])


class MetricsMiddleware:
    def __init__(self, app, application):
        self.app = app
        self.application = application
        self.routes = None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        status = 500
        start = time.monotonic()

        async def measure(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, measure)
        finally:
            if self.routes is None:
                self.routes = [
                    (path, compile_path(path)[0]) for path in self.application.openapi()["paths"]
                ]
            route = next(
                (path for path, pattern in self.routes if pattern.fullmatch(scope["path"])),
                "unmatched",
            )
            if scope["path"] != "/api/v1/metrics":
                REQUESTS.labels(scope["method"], route, str(status)).inc()
                LATENCY.labels(scope["method"], route).observe(time.monotonic() - start)
