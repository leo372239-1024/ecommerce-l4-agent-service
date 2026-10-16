"""Completed prospective V4; known regressions never inflate new-composition metrics."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from analyze_memory_v2 import complete_chat_usage
from analyze_step10 import call_usage, load, outcomes, target

ROOT = Path(__file__).resolve().parents[1]


def main():
    directory = ROOT / "artifacts/benchmarks/step10-memory-final-v4"
    assert (directory / "summary.json").exists(), "complete schedule required"
    rows = load(directory / "case-results.jsonl")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    key = lambda row: (row["case_id"], row["variant"], row["trial"])
    assert len(rows) == len({key(r) for r in rows}) == len(manifest["schedule"]) == 384
    assert {key(r) for r in rows} == {key(r) for r in manifest["schedule"]}
    primary_rows = [r for r in rows if int(r["case_id"][2:]) <= 60]
    known_rows = [r for r in rows if int(r["case_id"][2:]) > 60]
    assert len(primary_rows) == 360 and len(known_rows) == 24
    calls = {r["workflow_id"]: r["calls"] for r in load(directory / "provider-calls.jsonl")}
    successes = {
        variant: {
            (r["case_id"], r["trial"])
            for r in primary_rows
            if r["variant"] == variant and r["strict_success"]
        }
        for variant in ("P", "M-vector")
    }
    common = successes["P"] & successes["M-vector"]
    missing = {
        (r["case_id"], r["trial"])
        for r in primary_rows
        if not complete_chat_usage(calls[r["workflow_id"]])
    }
    complete = common - missing
    paired = [r for r in primary_rows if (r["case_id"], r["trial"]) in complete]
    paired_usage = call_usage(paired, directory) if paired else {}
    reduction = (
        1 - paired_usage["P"]["chat_prompt_tokens"] / paired_usage["M-vector"]["chat_prompt_tokens"]
        if paired and paired_usage["M-vector"]["chat_prompt_tokens"]
        else None
    )
    primary = outcomes(primary_rows, "M-vector")
    targets = {
        **target(primary, 0.08),
        "absolute_quality_minimum": 0.9,
        "absolute_quality_met": primary["groups"]["P"]["success_rate"] >= 0.9,
        "conditional_input_token_minimum_reduction": 0.25,
        "conditional_input_minimum_pairs": 30,
        "conditional_input_target_met": len(complete) >= 30
        and reduction is not None
        and reduction >= 0.25,
    }
    targets["joint_registered_metrics_met"] = all(
        targets[k]
        for k in (
            "point_target_met",
            "positive_effect_supported_95",
            "absolute_quality_met",
            "conditional_input_target_met",
        )
    )
    source = json.loads((directory / "source-manifest.json").read_text(encoding="utf-8"))
    assert (
        hashlib.sha256(
            (directory / "operational-source/offline_prepare_retry.py").read_bytes()
        ).hexdigest()
        == source["source_hashes"]["scripts/offline_prepare_retry.py"]
    )
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "experiment": directory.name,
        "episodes": 384,
        "new_primary_episodes": 360,
        "known_regression_episodes": 24,
        "primary": primary,
        "anaphoric": outcomes([r for r in primary_rows if int(r["case_id"][2:]) <= 48], "M-vector"),
        "explicit_controls": outcomes(
            [r for r in primary_rows if int(r["case_id"][2:]) > 48], "M-vector"
        ),
        "known_carrier_regressions": outcomes(known_rows, "M-vector"),
        "usage_all_arrivals": call_usage(rows, directory),
        "usage_primary": call_usage(primary_rows, directory),
        "registered_conditional_input": {
            "both_success_pairs": len(common),
            "complete_usage_pairs": len(complete),
            "excluded_missing_usage_pairs": len(common - complete),
            "coverage_of_all_primary_pairs": len(complete) / 180,
            "usage": paired_usage,
            "reduction": reduction,
            "scope": "New 60-case primary only; same case/trial correctly completed by both groups with complete reported chat input, not all-arrival invoice or offline embedding cost.",
        },
        "target": targets,
        "registered_offline_wrapper_matches_frozen_source": True,
        "raw_results_sha256": hashlib.sha256(
            (directory / "case-results.jsonl").read_bytes()
        ).hexdigest(),
        "scope": "New compositions with exposed terminology disclosed, synthetic past turns and real current model/MCP. Known carrier regressions excluded from primary gains; no independent human blind review; preceding results unchanged.",
    }
    output = ROOT / "artifacts/benchmarks/step10-analysis-memory-final-v4.json"
    assert not output.exists(), "preserve first full analysis"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(targets, indent=2))


if __name__ == "__main__":
    main()
