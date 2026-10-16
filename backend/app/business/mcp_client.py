import json

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from opentelemetry import trace
from opentelemetry.propagate import inject

from app.agent.tools import ToolError
from app.business.catalog import CATALOG, canonical_hash
from app.security.guards import safe_output, signals


class MerchantClient:
    def __init__(self, settings, run):
        self.settings, self.run = settings, run

    async def invoke(self, tool, args):
        try:
            with trace.get_tracer("merchant-gateway").start_as_current_span(
                "gateway.mcp",
                attributes={
                    "agent.workflow_id": str(self.run["id"]),
                    "agent.tool_name": tool,
                    "agent.fencing": self.run["fencing"],
                },
            ):
                return await self._invoke(tool, args)
        except Exception as exc:  # noqa: BLE001 - SDK task-group boundary; preserve safe business codes

            def leaves(error):
                if isinstance(error, ExceptionGroup):
                    return [leaf for child in error.exceptions for leaf in leaves(child)]
                return [error]

            errors = leaves(exc)
            business = [error for error in errors if isinstance(error, ToolError)]
            if len(business) == len(errors):
                raise business[0] from None
            raise ToolError("upstream_unavailable", True) from None

    async def _invoke(self, tool, args):
        if tool not in CATALOG:
            raise ToolError("tool_not_allowed")
        # The URL is deployment configuration; never supplied by a user/model/document.
        trace_headers = {}
        inject(trace_headers)
        async with (
            httpx2.AsyncClient(
                headers={
                    "Authorization": "Bearer " + self.settings.mcp_service_key.get_secret_value(),
                    "X-Workflow-Id": str(self.run["id"]),
                    "X-Execution-Fence": str(self.run["fencing"]),
                    **trace_headers,
                },
                timeout=10,
                follow_redirects=False,
            ) as http,
            Client(
                streamable_http_client(self.settings.mcp_url, http_client=http),
                read_timeout_seconds=10,
            ) as client,
        ):
            listed = await client.list_tools()
            remote = next((t for t in listed.tools if t.name == tool), None)
            if not remote:
                raise ToolError("remote_tool_missing")
            # SDK-generated wrappers include annotations/titles. Compare semantic input shape.
            schema = remote.input_schema
            expected = CATALOG[tool].schema
            if canonical_hash(schema) != canonical_hash(expected):
                raise ToolError("remote_schema_drift")
            if remote.description != CATALOG[tool].description:
                raise ToolError("remote_description_drift")
            result = await client.call_tool(tool, args)
            if result.is_error:
                content = " ".join(getattr(item, "text", "") for item in result.content)
                # Allowlisted safe business error codes only; no raw remote text is persisted.
                codes = [
                    "stale_execution_authority",
                    "permission_revoked",
                    "order_unavailable",
                    "upstream_unavailable",
                    "business_data_unavailable",
                    "approval_invalid_or_stale",
                    "supervisor_authority_revoked",
                    "business_precondition_failed",
                    "operation_contract_mismatch",
                    "operation_argument_conflict",
                    "money_contract_mismatch",
                    "run_cancelled",
                    "invalid_business_data",
                ]
                raise ToolError(
                    next((code for code in codes if code in content), "mcp_execution_rejected")
                )
            value = result.structured_content
            if value is None:
                value = json.loads(
                    next(item.text for item in result.content if hasattr(item, "text"))
                )
            # Some SDK versions wrap dict-return values under `result`.
            if "schema_version" not in value and isinstance(value.get("result"), dict):
                value = value["result"]
            if value.get("schema_version") != 1 or not isinstance(value.get("fields"), dict):
                raise ToolError("invalid_evidence")
            if signals(value["fields"]):
                raise ToolError("unsafe_tool_content")
            return safe_output(value)
