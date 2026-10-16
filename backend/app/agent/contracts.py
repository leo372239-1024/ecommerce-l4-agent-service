"""Executable contracts: the model proposes intent; code owns effects and budgets."""

import hashlib
import json
import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.security.guards import valid_text


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def text_contract(cls, value):
        def walk(item):
            if isinstance(item, str):
                valid_text(item)
            elif isinstance(item, dict):
                for key, child in item.items():
                    walk(key)
                    walk(child)
            elif isinstance(item, list):
                for child in item:
                    walk(child)

        walk(value)
        return value


class Goal(StrEnum):
    ORDER = "order"
    LOGISTICS = "logistics"
    ELIGIBILITY = "eligibility"
    QUOTE = "refund_quote"
    WRITE = "business_write"
    REFUND = "submit_refund"
    RETURN = "create_return"
    CANCEL = "cancel_order"
    QUERY = "business_query"
    KNOWLEDGE = "knowledge"
    UNSUPPORTED = "unsupported"


class OrderGoals(StrictModel):
    order_id: str
    goals: list[Goal] = Field(min_length=1, max_length=10)


class QuerySpec(StrictModel):
    order_id: str
    query: str = Field(
        min_length=2,
        max_length=1000,
        description="当前任务原文中的业务属性连续片段，禁止改写或补造实体句子；共同属性可为多个order_id重复使用相同片段。",
    )


class TaskSpec(StrictModel):
    order_ids: list[str] = Field(default_factory=list, max_length=3)
    goals: list[Goal] = Field(min_length=1, max_length=10)
    missing_inputs: list[Literal["order_id"]] = Field(default_factory=list, max_length=1)
    order_goals: list[OrderGoals] = Field(default_factory=list, max_length=3)
    queries: list[QuerySpec] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def validate_scope(self):
        knowledge_queries = [query for query in self.queries if not query.order_id]
        business_queries = [query for query in self.queries if query.order_id]
        if knowledge_queries and Goal.KNOWLEDGE not in self.goals:
            raise ValueError("query_scope_mismatch")
        if Goal.KNOWLEDGE in self.goals and not knowledge_queries:
            raise ValueError("missing_specific_queries")
        if self.order_goals:
            ids = [target.order_id for target in self.order_goals]
            if len(set(ids)) != len(ids) or set(ids) != set(self.order_ids):
                raise ValueError("incomplete_order_scope")
            target_goals = {goal for target in self.order_goals for goal in target.goals}
            if target_goals != set(self.goals) - {Goal.KNOWLEDGE}:
                raise ValueError("goal_scope_mismatch")
        query_targets = (
            {target.order_id for target in self.order_goals if Goal.QUERY in target.goals}
            if self.order_goals
            else set(self.order_ids)
            if Goal.QUERY in self.goals
            else set()
        )
        if business_queries:
            if {query.order_id for query in business_queries} != query_targets:
                raise ValueError("query_scope_mismatch")
            if len({(query.order_id, query.query) for query in self.queries}) != len(self.queries):
                raise ValueError("duplicate_query")
        elif query_targets:
            raise ValueError("missing_specific_queries")
        return self


class ResumeInput(StrictModel):
    order_ids: list[str] = Field(min_length=1, max_length=3)

    @field_validator("order_ids")
    @classmethod
    def valid_ids(cls, values):
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,63}", item) for item in values):
            raise ValueError("invalid_order_id")
        return list(dict.fromkeys(values))


class ApprovalResume(StrictModel):
    operation_id: str = Field(pattern=r"^[0-9a-f-]{36}$")


class Critique(StrictModel):
    issue: Literal["stale_environment", "upstream_unavailable", "unrecoverable"]
    action: Literal["refresh_order", "retry_read", "handoff"]


class PlanNode(StrictModel):
    id: str
    goal: Goal
    tool: Literal[
        "get_order",
        "get_logistics",
        "get_policy",
        "check_eligibility",
        "quote_refund",
        "business_query",
        "submit_refund",
        "create_return",
        "cancel_order",
    ]
    order_id: str
    dependencies: list[str]
    preconditions: list[Literal["owned_order", "facts_available", "policy_available", "eligible"]]
    postconditions: list[Literal["versioned_evidence", "currency_cny", "nonnegative_minor_units"]]
    risk: Literal["read", "write"] = "read"
    query: str = Field(default="", max_length=1000)


class Plan(StrictModel):
    version: int = 1
    nodes: list[PlanNode] = Field(max_length=32)

    @model_validator(mode="after")
    def validate_dag(self):
        ids = {node.id for node in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError("duplicate_node")
        done = set()
        required = {
            "get_order": [],
            "get_logistics": ["get_order"],
            "get_policy": ["get_order"],
            "check_eligibility": ["get_order", "get_policy"],
            "quote_refund": ["get_order", "get_policy", "check_eligibility"],
            "business_query": ["get_order"],
            "create_return": ["get_order", "get_policy", "check_eligibility"],
            "submit_refund": ["get_order", "get_policy", "check_eligibility", "quote_refund"],
            "cancel_order": ["get_order", "get_policy"],
        }
        for node in self.nodes:
            if not set(node.dependencies) <= done or node.id in node.dependencies:
                raise ValueError("non_topological_or_cyclic_plan")
            if node.tool != "get_order" and "owned_order" not in node.preconditions:
                raise ValueError("missing_ownership_guard")
            if set(node.dependencies) != {
                f"{node.order_id}:{tool}" for tool in required[node.tool]
            }:
                raise ValueError("missing_mandatory_dependency")
            if node.tool == "business_query":
                if not re.fullmatch(
                    re.escape(node.order_id) + r":business_query(?::(?:[0-9]|1[01]))?", node.id
                ):
                    raise ValueError("invalid_query_node_identity")
            elif node.id != f"{node.order_id}:{node.tool}":
                raise ValueError("invalid_node_identity")
            done.add(node.id)
            if (node.tool in {"create_return", "submit_refund", "cancel_order"}) != (
                node.risk == "write"
            ):
                raise ValueError("risk_effect_mismatch")
        return self


def compile_plan(spec: TaskSpec) -> Plan:
    nodes = []
    for order_id in dict.fromkeys(spec.order_ids):
        goals = next(
            (target.goals for target in spec.order_goals if target.order_id == order_id), spec.goals
        )

        def add(tool, goal, deps, pre, post=None, order_id=order_id):
            node = PlanNode(
                id=f"{order_id}:{tool}",
                goal=goal,
                tool=tool,
                order_id=order_id,
                dependencies=[f"{order_id}:{dep}" for dep in deps],
                preconditions=pre,
                postconditions=post or ["versioned_evidence"],
                risk="write"
                if tool in {"create_return", "submit_refund", "cancel_order"}
                else "read",
            )
            nodes.append(node)

        if set(goals) <= {Goal.WRITE, Goal.UNSUPPORTED, Goal.KNOWLEDGE}:
            continue
        add("get_order", Goal.ORDER, [], [])
        if Goal.LOGISTICS in goals:
            add("get_logistics", Goal.LOGISTICS, ["get_order"], ["owned_order", "facts_available"])
        if Goal.QUERY in goals:
            queries = [q for q in spec.queries if q.order_id == order_id]
            if queries:
                for index, query in enumerate(queries):
                    nodes.append(
                        PlanNode(
                            id=f"{order_id}:business_query:{index}",
                            goal=Goal.QUERY,
                            tool="business_query",
                            order_id=order_id,
                            dependencies=[f"{order_id}:get_order"],
                            preconditions=["owned_order"],
                            postconditions=["versioned_evidence"],
                            query=query.query,
                        )
                    )
            else:
                add("business_query", Goal.QUERY, ["get_order"], ["owned_order"])
        if set(goals) & {Goal.ELIGIBILITY, Goal.QUOTE, Goal.REFUND, Goal.RETURN, Goal.CANCEL}:
            add("get_policy", Goal.ELIGIBILITY, ["get_order"], ["owned_order"])
        if set(goals) & {Goal.ELIGIBILITY, Goal.QUOTE, Goal.REFUND, Goal.RETURN}:
            add(
                "check_eligibility",
                Goal.ELIGIBILITY,
                ["get_order", "get_policy"],
                ["owned_order", "facts_available", "policy_available"],
            )
        if set(goals) & {Goal.QUOTE, Goal.REFUND}:
            add(
                "quote_refund",
                Goal.QUOTE,
                ["get_order", "get_policy", "check_eligibility"],
                ["owned_order", "facts_available", "policy_available", "eligible"],
                ["versioned_evidence", "currency_cny", "nonnegative_minor_units"],
            )
        for goal, deps in (
            (Goal.RETURN, ["get_order", "get_policy", "check_eligibility"]),
            (Goal.REFUND, ["get_order", "get_policy", "check_eligibility", "quote_refund"]),
            (Goal.CANCEL, ["get_order", "get_policy"]),
        ):
            if goal in goals:
                add(goal.value, goal, deps, ["owned_order", "facts_available", "policy_available"])
    return Plan(nodes=nodes)


def progress_signature(plan_version, remaining, evidence, error):
    value = [
        plan_version,
        sorted(remaining),
        sorted((key, item["entity_version"]) for key, item in evidence.items()),
        error,
    ]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
