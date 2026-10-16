"""Transient gateway faults may retry inference, within the remaining model-call budget."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent.planner import Planner, PlanningError
from app.core.config import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statuses,limit,expected_calls,success",
    [
        ([503, 200], 3, 2, True),
        ([429, 503, 200], 3, 3, True),
        ([401, 200], 3, 1, False),
        ([400, 200], 3, 1, False),
        ([503, 503, 503, 200], 3, 3, False),
        ([503, 200], 1, 1, False),
        ([200], 0, 0, False),
    ],
)
async def test_inference_transport_retries_are_bounded_and_auth_errors_are_terminal(
    monkeypatch, statuses, limit, expected_calls, success
):
    requests = []
    spec = {
        "order_ids": ["ORD-TEST"],
        "goals": ["order"],
        "missing_inputs": [],
        "order_goals": [{"order_id": "ORD-TEST", "goals": ["order"]}],
        "queries": [],
    }

    def handler(request):
        requests.append(request)
        status = statuses[len(requests) - 1]
        return httpx.Response(
            status,
            json={
                "usage": {"total_tokens": 23},
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "submit_task",
                                        "arguments": json.dumps(spec),
                                    }
                                }
                            ]
                        }
                    }
                ],
            },
        )

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client_class(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr("app.agent.planner.asyncio.sleep", AsyncMock())
    planner = Planner(Settings(_env_file=None, _secrets_dir=None))
    if success:
        result, usage = await planner.parse("查询ORD-TEST的订单状态", max_attempts=limit)
        assert result.order_ids == ["ORD-TEST"]
        assert usage["llm_calls"] == expected_calls and usage["total_tokens"] == 23
    else:
        with pytest.raises(PlanningError) as error:
            await planner.parse("查询ORD-TEST的订单状态", max_attempts=limit)
        assert str(error.value) == ("model_budget" if limit == 0 else "model_unavailable")
        assert error.value.usage["llm_calls"] == expected_calls
        if limit:
            assert error.value.usage["provider_http_status"] == statuses[expected_calls - 1]
            assert error.value.usage["usage_missing"]
    assert len(requests) == expected_calls
