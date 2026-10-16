from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


async def validation_error(request, exc: RequestValidationError):
    # Pydantic's default includes raw input and can echo passwords, documents or tokens.
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {"type": e["type"], "loc": e["loc"], "msg": "invalid_request_field"}
                for e in exc.errors()
            ]
        },
    )


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def secured(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"cache-control", b"no-store"),
                    ]
                )
            await send(message)

        await self.app(scope, receive, secured)
