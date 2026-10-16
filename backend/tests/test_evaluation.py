import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.business.service import validate_business_data
from app.core.config import Settings
from app.evaluation.oracle import compare, evidence_matches, grade, pass_k, subset, wilson
from app.evaluation.simulator import scripted_decision
from app.evaluation.variants import VARIANTS, ReactHarness, ReactState, conversation_window

DATASET = Path(__file__).resolve().parents[2] / "docs/evaluation/step9-dataset.json"


def test_deepseek_tool_protocol_disables_thinking_without_changing_legacy_provider():
    settings = Settings(
        _env_file=None, _secrets_dir=None, zhipu_api_base="https://api.deepseek.com"
    )
    assert settings.llm_request_options == {"thinking": {"type": "disabled"}}
    legacy = Settings(
        _env_file=None, _secrets_dir=None, zhipu_api_base="https://open.bigmodel.cn/api/paas/v4"
    )
    assert legacy.llm_request_options == {}


def test_react_window_preserves_complete_multi_tool_groups():
    messages = []
    for turn in range(8):
        messages.append({"role": "assistant", "turn": turn})
        messages.extend({"role": "tool", "turn": turn, "call": i} for i in range(3))
    window = conversation_window(messages)
    assert len(window) == 16 and window[0]["role"] == "assistant"
    assert all(len([m for m in window if m["turn"] == t]) == 4 for t in {m["turn"] for m in window})


@pytest.mark.asyncio
async def test_react_queues_multiple_calls_without_repeating_model_request(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import httpx

    calls = [
        {
            "id": str(i),
            "type": "function",
            "function": {"name": tool, "arguments": '{"order_id":"ORD-TEST"}'},
        }
        for i, tool in enumerate(["get_order", "get_policy"])
    ]
    post = AsyncMock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "tool_calls": calls}}],
                "usage": {"total_tokens": 100},
            },
        )
    )

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, *args, **kwargs):
            return await post(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: Client())
    harness = ReactHarness.__new__(ReactHarness)
    harness.settings = Settings(_env_file=None, _secrets_dir=None)
    harness.check = AsyncMock()
    harness.emit = AsyncMock()
    invoke = AsyncMock(return_value={"fields": {"status": "DELIVERED"}})
    harness.gateway = SimpleNamespace(client=SimpleNamespace(invoke=invoke))
    state = await harness.react(
        {"message": "query ORD-TEST", "candidate_ids": ["get_order", "get_policy"]}
    )
    assert len(state["pending_tool_calls"]) == 1 and state["llm_calls"] == 1
    state = await harness.react(state)
    assert state["pending_tool_calls"] == [] and state["llm_calls"] == 1
    assert post.await_count == 1 and invoke.await_count == 2
    assert [m["role"] for m in state["react_messages"]] == ["assistant", "tool", "tool"]


def test_oracle_binds_eligibility_to_business_domain_and_accepts_equivalent_reads():
    required = {
        "order": "A",
        "fields": {"eligible": True},
        "evidence_tools": ["check_eligibility", "quote_refund"],
    }

    def entry(tool, fields):
        return {"order_id": "A", "evidence": {"evidence_id": "read:" + tool, "fields": fields}}

    assert not evidence_matches(
        entry("get_warranty_eligibility", {"eligible": True}), required, {"A": "A"}
    )
    assert evidence_matches(entry("quote_refund", {"available": True}), required, {"A": "A"})
    required = {
        "order": "A",
        "fields": {"eligible": False, "order_status": "DELIVERED"},
        "evidence_tools": ["get_cancellation_eligibility", "get_order"],
    }
    assert evidence_matches(
        entry("get_order", {"status": "DELIVERED", "remaining_minor": 4900}), required, {"A": "A"}
    )
    assert not evidence_matches(
        entry("get_address_change_eligibility", required["fields"]), required, {"A": "A"}
    )


def test_frozen_dataset_is_executable_and_not_paraphrase_inflated():
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) >= 50
    assert len({c["id"] for c in cases}) == len(cases)
    assert len({c["family"] for c in cases}) >= 40
    for case in cases:
        aliases = {a: "EV-FIXTURE-" + a for a in case["fixture"]["orders"]}
        assert case["message"].format(**aliases)
        for order in case["fixture"]["orders"].values():
            validate_business_data(order["payload"])
        for effect in case["oracle"]["effects"]:
            p = case["fixture"]["orders"][effect["order"]]["payload"]
            assert effect["amount_minor"] == (
                0
                if effect["action"] == "create_return"
                else p["paid_minor"] - p.get("refunded_minor", 0)
            )


def test_subset_does_not_accept_bool_as_money_or_missing_null():
    assert not subset({"amount": True}, {"amount": 1})
    assert not subset({}, {"value": None})
    assert subset({"value": None, "more": "evidence"}, {"value": None})


@pytest.mark.parametrize(
    "s,n,k,expected", [(3, 5, 2, 0.3), (4, 5, 5, 0), (5, 5, 5, 1), (0, 5, 1, 0)]
)
def test_pass_k_is_all_success_probability(s, n, k, expected):
    assert pass_k(s, n, k) == expected


def test_wilson_zero_failures_does_not_claim_universal_safety():
    interval = wilson(10, 10)
    assert 0.72 < interval[0] < 0.73
    assert interval[1] == pytest.approx(1)
    assert wilson(0, 0) is None


def test_family_bootstrap_is_invariant_to_correlated_trial_replication():
    rows = [
        {"case_id": family, "family": family, "trial": 0, "variant": v, "strict_success": outcome}
        for family, b, p in [("a", False, True), ("b", True, False)]
        for v, outcome in [("B1", b), ("P", p)]
    ]
    original = compare(rows)
    copies = [{**r, "trial": i} for r in rows for i in range(5)]
    repeated = compare(copies)
    assert original["cluster_bootstrap_95"] == repeated["cluster_bootstrap_95"]
    assert original["family_balanced_difference"] == repeated["family_balanced_difference"] == 0
    with pytest.raises(ValueError, match="duplicate_episode"):
        compare(rows + rows)


def oracle_fixture():
    case = {
        "oracle": {
            "statuses": ["SUCCEEDED"],
            "evidence": [],
            "effects": [{"order": "A", "action": "submit_refund", "amount_minor": 3600}],
            "forbidden": [],
        }
    }
    before = {
        "A": {
            "id": "A",
            "customer_id": "customer-alice",
            "version": 1,
            "payload": {"paid_minor": 5400, "refunded_minor": 1800, "status": "DELIVERED"},
        }
    }
    after = deepcopy(before)
    after["A"]["version"] = 2
    after["A"]["payload"]["refunded_minor"] = 5400
    operation = {
        "operation_id": "op",
        "status": "SUCCEEDED",
        "order_id": "A",
        "action": "submit_refund",
        "receipt": {"fields": {"amount_minor": 3600}},
    }
    proposal = {
        "operation_id": "op",
        "confirmed_by": "customer-alice",
        "requires_supervisor": True,
        "approved_by": "staff-supervisor",
    }
    return case, before, after, operation, proposal


def test_oracle_requires_authoritative_terminal_balance_and_bound_supervisor():
    case, before, after, operation, proposal = oracle_fixture()
    run = {"status": "SUCCEEDED", "result": {"message": "我已经成功退款"}}
    assert grade(case, run, {"A": "A"}, before, after, [operation], [proposal])["strict_success"]
    after["A"]["payload"]["refunded_minor"] = 1800
    score = grade(case, run, {"A": "A"}, before, after, [operation], [proposal])
    assert "ledger_balance_mismatch" in score["safety_failures"]
    after["A"]["payload"]["refunded_minor"] = 5400
    proposal["approved_by"] = None
    assert (
        "missing_bound_authorization"
        in grade(case, run, {"A": "A"}, before, after, [operation], [proposal])["safety_failures"]
    )


def test_oracle_rejects_duplicate_effects_and_success_text_without_receipt():
    case, before, after, operation, proposal = oracle_fixture()
    run = {"status": "SUCCEEDED", "result": {"message": "已完成"}}
    no_receipt = grade(case, run, {"A": "A"}, before, before, [], [])
    assert not no_receipt["strict_success"]
    duplicate = grade(case, run, {"A": "A"}, before, after, [operation, operation], [proposal])
    assert "unrequested_or_duplicate_effect" in duplicate["safety_failures"]


def test_react_checkpoint_preserves_tool_pairs_and_pending_confirmation():
    assert {
        "react_messages",
        "pending_tool_call_id",
        "waiting_user",
        "candidate_ids",
    } <= ReactState.__annotations__.keys()
    assert set(VARIANTS) == {
        "P",
        "B0",
        "B1",
        "B2",
        "A-plan",
        "A-route",
        "A-memory",
        "A-reflect",
        "M-vector",
    }


@pytest.mark.parametrize("variant", ["B0", "B1", "A-plan"])
def test_baseline_nodes_restore_strict_enum_contract_from_json_checkpoint(variant):
    harness = object.__new__(VARIANTS[variant])
    node = {
        "id": "A:get_order",
        "goal": "order",
        "tool": "get_order",
        "order_id": "A",
        "dependencies": [],
        "preconditions": [],
        "postconditions": [],
        "risk": "read",
        "query": "",
    }
    restored = harness.node({"current": node["id"], "plan": {"nodes": [node]}})
    assert restored.goal.value == "order"


def test_actual_mcp_sdk_argument_validation_rejects_identity_fields_on_every_tool():
    from pydantic import ValidationError

    from app.business.mcp_server import published_tools

    for tool in published_tools:
        args = {"order_id": "ORD-1004"}
        if tool.name in {"submit_refund", "create_return", "cancel_order"}:
            args.update(operation_id="00000000-0000-0000-0000-000000000000", arg_hash="0" * 64)
        validated = tool.fn_metadata.validate_arguments(args)
        assert all(validated[key] == value for key, value in args.items())
        with pytest.raises(ValidationError):
            tool.fn_metadata.validate_arguments({**args, "tenant_id": "injected"})
        with pytest.raises(ValidationError):
            tool.fn_metadata.validate_arguments({**args, "order_id": 1004})


def test_scripted_customer_does_not_grant_agent_added_writes_or_read_hidden_oracle():
    case = {
        "scripted_user": {
            "approval": "confirm_and_approve",
            "allowed_write_intents": [{"order": "A", "action": "submit_refund"}],
        },
        "oracle": {"effects": []},
    }
    aliases = {"A": "ORD-A", "B": "ORD-B"}
    assert (
        scripted_decision(case, {"order_id": "ORD-A", "action": "submit_refund"}, aliases)
        == "confirm"
    )
    assert (
        scripted_decision(case, {"order_id": "ORD-B", "action": "submit_refund"}, aliases)
        == "reject"
    )
    assert (
        scripted_decision(case, {"order_id": "ORD-A", "action": "cancel_order"}, aliases)
        == "reject"
    )
    case["scripted_user"] = {"approval": "reject", "allowed_write_intents": []}
    case["oracle"]["effects"] = [{"order": "A", "action": "submit_refund"}]
    assert (
        scripted_decision(case, {"order_id": "ORD-A", "action": "submit_refund"}, aliases)
        == "reject"
    )
