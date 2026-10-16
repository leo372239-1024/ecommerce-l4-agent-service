from fastapi import APIRouter, Request
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import Response

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    # DB-backed fleet gauges survive worker restarts; never label by customer/run/order.
    lines = []
    try:
        async with request.app.state.resources.engine.connect() as conn:
            counts = (
                await conn.execute(text("SELECT status,count(*) FROM agent_runs GROUP BY status"))
            ).all()
            expired = await conn.scalar(
                text("SELECT count(*) FROM agent_runs WHERE status='RUNNING' AND lease_until<now()")
            )
            outbox = await conn.scalar(
                text("SELECT count(*) FROM agent_outbox WHERE delivered_at IS NULL")
            )
            operations = dict(
                (
                    await conn.execute(
                        text("SELECT status,count(*) FROM business_operations GROUP BY status")
                    )
                ).all()
            )
            merchant_pending = await conn.scalar(
                text("SELECT count(*) FROM merchant_outbox WHERE delivered_at IS NULL")
            )
        lines = [
            "# HELP agent_runs_count Durable workflow count by status",
            "# TYPE agent_runs_count gauge",
        ]
        allowed = {
            "QUEUED",
            "RUNNING",
            "WAITING_USER",
            "WAITING_APPROVAL",
            "SUCCEEDED",
            "PARTIAL",
            "HANDOFF",
            "FAILED",
            "CANCELLED",
        }
        by_status = dict(counts)
        lines.extend(
            f'agent_runs_count{{status="{status}"}} {by_status.get(status, 0)}'
            for status in sorted(allowed)
        )
        lines += [
            "# TYPE agent_merchant_outbox_pending gauge",
            f"agent_merchant_outbox_pending {merchant_pending}",
            "# TYPE agent_business_operations_count gauge",
            *[
                f'agent_business_operations_count{{status="{status}"}} {operations.get(status, 0)}'
                for status in ("PREPARED", "DISPATCHED", "RECONCILING", "SUCCEEDED", "REJECTED")
            ],
            "# TYPE agent_expired_leases gauge",
            f"agent_expired_leases {expired}",
            "# TYPE agent_outbox_pending gauge",
            f"agent_outbox_pending {outbox}",
            "# TYPE agent_db_metrics_available gauge",
            "agent_db_metrics_available 1",
        ]
    except (SQLAlchemyError, OSError):
        lines = ["# TYPE agent_db_metrics_available gauge", "agent_db_metrics_available 0"]
    return Response(
        content=generate_latest() + ("\n".join(lines) + "\n").encode(),
        media_type=CONTENT_TYPE_LATEST,
    )
