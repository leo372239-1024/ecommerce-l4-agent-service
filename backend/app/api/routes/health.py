from datetime import UTC, datetime

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

router = APIRouter()


@router.get("/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok", "timestamp": datetime.now(UTC).isoformat()}


@router.get("/ready")
async def readiness(request: Request) -> JSONResponse:
    checks = await request.app.state.resources.checks()
    ready = all(value == "ok" for value in checks.values())
    return JSONResponse(
        {
            "status": "ok" if ready else "unavailable",
            "checks": checks,
            "timestamp": datetime.now(UTC).isoformat(),
        },
        status_code=200 if ready else 503,
    )
