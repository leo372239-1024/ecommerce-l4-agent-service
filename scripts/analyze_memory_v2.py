"""Read-only analysis of the prospectively registered memory V2 schedule."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from analyze_step10 import call_usage, load, outcomes, target

ROOT = Path(__file__).resolve().parents[1]


def complete_chat_usage(calls):
    chat = [c for c in calls if c["provider"] == "openrouter.ai"]
    return bool(chat) and all(
        type((c.get("usage") or {}).get("prompt_tokens")) is int
        and c["usage"]["prompt_tokens"] >= 0
        for c in chat
    )


def main(args):
    DIRECTORY = ROOT / "artifacts/benchmarks" / args.experiment
    assert (DIRECTORY / "summary.json").exists(), "completion marker absent"
    rows = load(DIRECTORY / "case-results.jsonl")
    manifest = json.loads((DIRECTORY / "manifest.json").read_text(encoding="utf-8"))
    key = lambda row: (row["case_id"], row["variant"], row["trial"])
    assert len(rows) == len({key(r) for r in rows}) == 360
    assert {key(r) for r in rows} == {key(r) for r in manifest["schedule"]}
    calls = {r["workflow_id"]: r["calls"] for r in load(DIRECTORY / "provider-calls.jsonl")}
    successful = {
        variant: {
            (r["case_id"], r["trial"])
            for r in rows
            if r["variant"] == variant and r["strict_success"]
        }
        for variant in ("P", "M-vector")
    }
    common = successful["P"] & successful["M-vector"]
    missing = {
        (r["case_id"], r["trial"]) for r in rows if not complete_chat_usage(calls[r["workflow_id"]])
    }
    complete = common - missing
    paired = [r for r in rows if (r["case_id"], r["trial"]) in complete]
    paired_usage = call_usage(paired, DIRECTORY) if paired else {}
    reduction = (
        1 - paired_usage["P"]["chat_prompt_tokens"] / paired_usage["M-vector"]["chat_prompt_tokens"]
        if paired and paired_usage["M-vector"]["chat_prompt_tokens"]
        else None
    )
    primary = outcomes(rows, "M-vector")
    absolute = primary["groups"]["P"]["success_rate"]
    targets = {
        **target(primary, 0.08),
        "absolute_quality_minimum": 0.90,
        "absolute_quality_met": absolute >= 0.90,
        "conditional_input_token_minimum_reduction": 0.25,
        "conditional_input_minimum_pairs": 30,
        "conditional_input_target_met": len(complete) >= 30
        and reduction is not None
        and reduction >= 0.25,
    }
    targets["joint_registered_metrics_met"] = (
        targets["point_target_met"]
        and targets["positive_effect_supported_95"]
        and targets["absolute_quality_met"]
        and targets["conditional_input_target_met"]
    )
    usage = call_usage(rows, DIRECTORY)
    source = json.loads((DIRECTORY / "source-manifest.json").read_text(encoding="utf-8"))
    wrapper = DIRECTORY / "operational-source/offline_prepare_retry.py"
    assert (
        hashlib.sha256(wrapper.read_bytes()).hexdigest()
        == source["source_hashes"]["scripts/offline_prepare_retry.py"]
    )
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "experiment": DIRECTORY.name,
        "episodes": 360,
        "primary": primary,
        "anaphoric": outcomes([r for r in rows if int(r["case_id"][2:]) <= 48], "M-vector"),
        "explicit_controls": outcomes([r for r in rows if int(r["case_id"][2:]) > 48], "M-vector"),
        "usage_all_arrivals": usage,
        "all_arrival_reported_input_reduction": 1
        - usage["P"]["chat_prompt_tokens"] / usage["M-vector"]["chat_prompt_tokens"],
        "registered_conditional_input": {
            "both_success_pairs": len(common),
            "complete_usage_pairs": len(complete),
            "excluded_missing_usage_pairs": len(common - complete),
            "coverage_of_all_pairs": len(complete) / 180,
            "usage": paired_usage,
            "reduction": reduction,
            "scope": "same case/trial correctly completed by both groups with complete API-reported chat input; not all-arrival billing or offline embedding cost",
        },
        "target": targets,
        "offline_transport": {
            "registered_before_inference": True,
            "wrapper_matches_frozen_source": True,
            "failed_attempts": load(DIRECTORY / "offline-retry-events.jsonl")
            if (DIRECTORY / "offline-retry-events.jsonl").exists()
            else [],
        },
        "raw_results_sha256": hashlib.sha256(
            (DIRECTORY / "case-results.jsonl").read_bytes()
        ).hexdigest(),
        "scope": "new project-authored compositions for the named registered version; shared updated Planner; no independent human blind review; synthetic past turns, real current model/MCP; no retroactive change to preceding version metrics",
    }
    output = args.output or ROOT / "artifacts/benchmarks/step10-analysis-memory-v2.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(targets, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--experiment",
        choices=["step10-memory-confirmation-v2", "step10-memory-final-v3"],
        default="step10-memory-confirmation-v2",
    )
    parser.add_argument("--output", type=Path)
    main(parser.parse_args())
