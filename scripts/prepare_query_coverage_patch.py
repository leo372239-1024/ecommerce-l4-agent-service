"""Stage and verify the known coverage fix; never edit the active frozen application."""

import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "artifacts/step10/v6-staged-source"
sys.path.insert(0, str(ROOT / "backend"))


def replace_once(source, before, after):
    assert source.count(before) == 1, "base source drift; do not guess patch location"
    return source.replace(before, after, 1)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    assert not STAGE.exists(), "preserve first candidate and provenance"
    STAGE.mkdir(parents=True)
    paths = {
        "intents.py": "backend/app/business/intents.py",
        "planner.py": "backend/app/agent/planner.py",
    }
    sources = {
        name: (ROOT / relative).read_text(encoding="utf-8") for name, relative in paths.items()
    }
    base = {
        relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        for relative in paths.values()
    }
    sources["intents.py"] = replace_once(
        sources["intents.py"],
        '"get_coupon_rules": r"优惠券(?:门槛|有效期|使用规则)",',
        '"get_coupon_rules": r"优惠券(?:使用)?(?:门槛|有效期)|优惠券使用规则",',
    )
    sources["intents.py"] = replace_once(
        sources["intents.py"],
        '"get_carrier_contact": r"承运(?:公司)?(?:客服)?(?:热线|联系方式)",',
        '"get_carrier_contact": r"承运(?:公司)?(?:客服)?(?:热线|联系方式)|(?:承运公司|承运商|物流公司)(?:名称)?(?=$|[和与及、，。！？\\s])",',
    )
    sources["planner.py"] = replace_once(
        sources["planner.py"],
        "from app.business.intents import split_query",
        "from app.business.intents import facets, split_query",
    )
    sources["planner.py"] = replace_once(
        sources["planner.py"],
        "独立业务投影分别生成queries，一个投影的多个属性可合并。",
        "独立业务投影分别生成queries，一个投影的多个属性可合并。所有明确请求的资料投影必须覆盖，遗漏会被程序拒绝。",
    )
    sources["planner.py"] = replace_once(
        sources["planner.py"],
        '    spec.missing_inputs = ["order_id"] if needs_order and not spec.order_ids else []\n    return spec',
        '    spec.missing_inputs = ["order_id"] if needs_order and not spec.order_ids else []\n'
        '    if spec.order_ids and not re.search(r"不要|无需|别查|不是|并非|不查", message):\n'
        "        requested = set(facets(message))\n"
        "        provided = {effect for query in spec.queries for effect in facets(query.query)}\n"
        "        if requested - provided:\n"
        '            raise PlanningError("requested_projection_omitted")\n'
        "    return spec",
    )
    for name, source in sources.items():
        compile(source, str(STAGE / name), "exec")
        (STAGE / name).write_text(source, encoding="utf-8")
    intents = load("app.business.intents", STAGE / "intents.py")
    planner = load("app.agent.planner", STAGE / "planner.py")
    checks = []
    for query, expected in (
        ("预计送达时间和承运公司", {"get_delivery_estimate", "get_carrier_contact"}),
        ("人工客服工作时间及优惠券使用门槛", {"get_support_hours", "get_coupon_rules"}),
    ):
        raw = {
            "order_ids": ["ORD-1002"],
            "goals": ["business_query"],
            "queries": [{"order_id": "ORD-1002", "query": query}],
        }
        spec = planner.grounded_spec(json.dumps(raw), "查询ORD-1002的" + query)
        actual = {intents.exact_effect(q.query) for q in spec.queries}
        checks.append({"query": query, "passed": actual == expected})
    raw = {
        "order_ids": ["ORD-1002"],
        "goals": ["business_query"],
        "queries": [{"order_id": "ORD-1002", "query": "预计送达时间"}],
    }
    try:
        planner.grounded_spec(json.dumps(raw), "查询ORD-1002的预计送达时间和发票类型")
        rejected = False
    except planner.PlanningError as error:
        rejected = str(error) == "requested_projection_omitted"
    checks.extend(
        [
            {"check": "reject_known_projection_omission", "passed": rejected},
            {
                "check": "carrier_policy_is_not_contact_projection",
                "passed": intents.exact_effect("承运公司赔付政策") is None,
            },
            {
                "check": "same_projection_attributes_remain_one_query",
                "passed": intents.split_query("商品材质和容量") == ["商品材质和容量"],
            },
            {
                "check": "negated_query_keeps_semantic_path",
                "passed": intents.exact_effect("不要查库存只查当前售价") is None,
            },
        ]
    )
    assert all(c["passed"] for c in checks), checks
    assert all(
        hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest
        for relative, digest in base.items()
    )
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "status": "staged_known_regressions_passed",
        "base_source_hashes": base,
        "candidate_source_hashes": {
            name: hashlib.sha256((STAGE / name).read_bytes()).hexdigest() for name in paths
        },
        "checks": checks,
        "active_source_unchanged": True,
        "scope": "Isolated Python module substitution in this process only; no API/model calls, deployed changes or blind quality evidence. Full tests and deployment still required after frozen v5 work completes.",
    }
    (STAGE / "provenance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"status": report["status"], "checks": len(checks), "active_source_unchanged": True}
        )
    )


if __name__ == "__main__":
    main()
