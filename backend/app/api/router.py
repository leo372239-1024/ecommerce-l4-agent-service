from fastapi import APIRouter

from app.api.routes.approvals import router as approvals_router
from app.api.routes.cases import router as cases_router
from app.api.routes.context import router as context_router
from app.api.routes.conversations import router as conversations_router
from app.api.routes.health import router as health_router
from app.api.routes.metrics import router as metrics_router
from app.api.routes.runs import router as runs_router
from app.security.oidc import router as oidc_router
from app.security.sandbox import router as sandbox_router

api_router = APIRouter()
api_router.include_router(health_router, prefix="/health", tags=["health"])
api_router.include_router(metrics_router, tags=["metrics"])
api_router.include_router(runs_router, tags=["agent"])
api_router.include_router(approvals_router, tags=["approvals"])
api_router.include_router(cases_router, tags=["human-cases"])
api_router.include_router(context_router, tags=["memory-knowledge-context"])
api_router.include_router(conversations_router, tags=["conversations"])
api_router.include_router(sandbox_router, tags=["sandbox"])
api_router.include_router(oidc_router, tags=["identity"])
