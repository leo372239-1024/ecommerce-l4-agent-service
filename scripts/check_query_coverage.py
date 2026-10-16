"""Known deterministic omissions; retain failing proof without changing frozen inference."""

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.agent.planner import PlanningError, grounded_spec
from app.business.intents import exact_effect


def main(args):
    checks = []
    for query, expected in (
        ("预计送达时间和承运公司", {"get_delivery_estimate", "get_carrier_contact"}),
        ("人工客服工作时间及优惠券使用门槛", {"get_support_hours", "get_coupon_rules"}),
    ):
        message = "查询ORD-1002的" + query
        raw = {
            "order_ids": ["ORD-1002"],
            "goals": ["business_query"],
            "queries": [{"order_id": "ORD-1002", "query": query}],
        }
        spec = grounded_spec(json.dumps(raw), message)
        actual = {exact_effect(q.query) for q in spec.queries}
        checks.append(
            {
                "query": query,
                "expected": sorted(expected),
                "actual": sorted(x for x in actual if x),
                "passed": actual == expected,
            }
        )
    raw = {
        "order_ids": ["ORD-1002"],
        "goals": ["business_query"],
        "queries": [{"order_id": "ORD-1002", "query": "预计送达时间"}],
    }
    rejected = False
    try:
        grounded_spec(json.dumps(raw), "查询ORD-1002的预计送达时间和发票类型")
    except PlanningError as error:
        rejected = str(error) == "requested_projection_omitted"
    checks.append({"check": "reject_known_requested_projection_omission", "passed": rejected})
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "status": "passed" if all(c["passed"] for c in checks) else "confirmed_defect",
        "checks": checks,
        "source_hashes": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ("backend/app/business/intents.py", "backend/app/agent/planner.py")
        },
        "scope": "Known development regressions, not new blind quality evidence; no API calls, runtime source changes or scoring modifications.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    assert not args.output.exists(), "preserve original failing proof"
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": report["status"], "passed": sum(c["passed"] for c in checks)}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(main(parser.parse_args()))
