"""Read-only paired analysis; refuses incomplete schedules and never changes raw evidence."""

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.evaluation.oracle import compare

EXPERIMENTS = {
    "plans": ("step10-openrouter-confirmation-v1", 1110, "B2"),
    "routing": ("step10-routing-confirmation-v1", 900, "R-hybrid"),
    "memory": ("step10-memory-confirmation-v1", 300, "M-vector"),
    "recovery": ("step10-recovery-confirmation-v1", 348, "A-reflect"),
}


def load(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def percentile(values, q):
    if not values:
        return None
    return sorted(values)[min(len(values) - 1, int((len(values) - 1) * q))]


def recovery_groups(cases):
    recoverable = {c["id"] for c in cases if c.get("recoverable")}
    persistent = {c["id"] for c in cases if c.get("fault") == "permanent_unavailable"}
    controls = {c["id"] for c in cases if not c.get("fault")}
    assert (len(recoverable), len(persistent), len(controls)) == (48, 8, 2)
    assert not (recoverable & persistent or recoverable & controls or persistent & controls)
    return recoverable, persistent, controls


def outcomes(rows, baseline):
    groups = {}
    for variant in (baseline, "P"):
        selected = [r for r in rows if r["variant"] == variant]
        assert selected
        successes = sum(r["strict_success"] for r in selected)
        families = defaultdict(list)
        for row in selected:
            families[row["family"]].append(int(row["strict_success"]))
        groups[variant] = {
            "episodes": len(selected),
            "cases": len({r["case_id"] for r in selected}),
            "successes": successes,
            "success_rate": successes / len(selected),
            "family_balanced_rate": sum(sum(v) / len(v) for v in families.values()) / len(families),
            "terminal_statuses": dict(Counter(r.get("status", "component") for r in selected)),
            "failures": dict(Counter(f for r in selected for f in r.get("failures", []))),
            "safety_failures": dict(
                Counter(f for r in selected for f in r.get("safety_failures", []))
            ),
            "latency_p50_seconds": percentile([r["elapsed_seconds"] for r in selected], 0.5),
            "latency_p95_seconds": percentile([r["elapsed_seconds"] for r in selected], 0.95),
            "prompt_tokens_reported": sum(r["usage"].get("prompt_tokens", 0) for r in selected),
            "history_preparation_seconds": sum(
                r.get("history_seed_and_offline_index_seconds", 0) for r in selected
            ),
        }
    # This exact bootstrap was registered before inference. Its generic old label is retained;
    # protocol status below distinguishes preregistration from independent blind validation.
    comparison = compare(rows, baseline, "P", seed=10001, repetitions=4000)
    comparison["protocol_status"] = "preregistered; not independently blind-reviewed"
    return {"groups": groups, "comparison": comparison}


def call_usage(rows, directory):
    if all("provider_calls" in r for r in rows):
        by_workflow = None
    else:
        by_workflow = {
            r["workflow_id"]: r["calls"] for r in load(directory / "provider-calls.jsonl")
        }
    groups = {}
    for variant in sorted({r["variant"] for r in rows}):
        selected = [r for r in rows if r["variant"] == variant]
        per_episode = [
            r["provider_calls"] if by_workflow is None else by_workflow[r["workflow_id"]]
            for r in selected
        ]
        calls = [
            c
            for r in selected
            for c in (r["provider_calls"] if by_workflow is None else by_workflow[r["workflow_id"]])
        ]
        chat = [c for c in calls if c["provider"] == "openrouter.ai"]
        costs = [(c.get("usage") or {}).get("cost") for c in chat]
        groups[variant] = {
            "episodes_without_reported_chat_request": sum(
                not any(c["provider"] == "openrouter.ai" for c in episode)
                for episode in per_episode
            ),
            "chat_requests": len(chat),
            "chat_prompt_tokens": sum((c.get("usage") or {}).get("prompt_tokens", 0) for c in chat),
            "chat_completion_tokens": sum(
                (c.get("usage") or {}).get("completion_tokens", 0) for c in chat
            ),
            "chat_prompt_usage_missing": sum(
                "prompt_tokens" not in (c.get("usage") or {}) for c in chat
            ),
            "reported_chat_cost": sum(v for v in costs if isinstance(v, (int, float))),
            "chat_cost_missing": sum(v is None for v in costs),
            "serving_providers": dict(
                Counter(c.get("serving_provider") or "unreported" for c in chat)
            ),
            "http_failure_calls": sum(
                c["status_code"] == 0 or c["status_code"] >= 400 for c in calls
            ),
            "retrieval_requests_including_offline_history": len(calls) - len(chat),
            "retrieval_usage_missing": sum(c.get("usage") is None for c in calls if c not in chat),
            "scope": "API-reported chat usage/cost; not reconciled invoice; offline embeddings excluded from input-Token gain but retrieval count includes them",
        }
    return groups


def analyze(name, root):
    experiment, count, baseline = EXPERIMENTS[name]
    directory = root / experiment
    marker = "complete.json" if name == "routing" else "summary.json"
    if not (directory / marker).exists():
        raise RuntimeError(f"{experiment}: completion marker absent")
    rows = load(directory / "case-results.jsonl")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    key_fields = (
        ("case_id", "variant", "trial", "catalog_size")
        if name == "routing"
        else ("case_id", "variant", "trial")
    )
    key = lambda r: tuple(r[k] for k in key_fields)
    keys = [key(r) for r in rows]
    assert len(rows) == count and len(set(keys)) == count, "incomplete or duplicate episodes"
    assert set(keys) == {key(r) for r in manifest["schedule"]}, "schedule mismatch"
    report = {"experiment": experiment, "episodes": count, "usage": call_usage(rows, directory)}
    if name == "plans":
        subsets = {
            "new_complex_primary": [r for r in rows if int(r["case_id"][2:]) <= 36],
            "all_novel": [r for r in rows if int(r["case_id"][2:]) <= 56],
            "known_edge_regressions": [r for r in rows if int(r["case_id"][2:]) > 56],
        }
        report["subsets"] = {k: outcomes(v, baseline) for k, v in subsets.items()}
        report["plan_ablation"] = outcomes(subsets["new_complex_primary"], "A-plan")
        primary = report["subsets"]["new_complex_primary"]
        report["target"] = target(primary, 0.08)
    elif name == "routing":
        report["catalogs"] = {
            str(size): outcomes([r for r in rows if r["catalog_size"] == size], baseline)
            for size in (50, 200, 1000)
        }
        report["targets_by_catalog"] = {
            size: target(result, 0.05) for size, result in report["catalogs"].items()
        }
        report["scope"] = (
            "component joint tool+all-arguments correctness; 40 merchant plus 960 non-executed public descriptors, three writes excluded in both groups"
        )
    elif name == "memory":
        amendment_path = directory / "operational-amendment.json"
        if amendment_path.exists():
            amendment = json.loads(amendment_path.read_text(encoding="utf-8"))
            prefix = (directory / "case-results.jsonl").read_bytes()[
                : amendment["scored_prefix_bytes"]
            ]
            assert hashlib.sha256(prefix).hexdigest() == amendment["scored_prefix_sha256"], (
                "previously scored rows changed"
            )
            wrapper = directory / "operational-source/offline_prepare_retry.py"
            assert (
                hashlib.sha256(wrapper.read_bytes()).hexdigest() == amendment["wrapper_sha256"]
            ), "offline wrapper changed"
            sidecar = directory / "offline-retry-events.jsonl"
            events = load(sidecar) if sidecar.exists() else []
            report["operational_amendment"] = {
                **amendment,
                "scored_prefix_integrity": True,
                "failed_physical_attempt_events": len(events),
                "extra_retry_attempts_not_in_original_final_attempt_metadata": sum(
                    e["disposition"] == "retry" for e in events
                ),
                "status_counts": dict(Counter(str(e["status_code"]) for e in events)),
                "scope": "post-registration offline preparation only; request payloads and agent inference/grade unchanged; additional failed-attempt billing unreported",
            }
        report["all_long_histories_primary"] = outcomes(rows, baseline)
        if amendment_path.exists():
            report["all_long_histories_primary"]["comparison"]["protocol_status"] = (
                "preregistered algorithm/data/scoring; post-registration offline transport amendment; not independently blind-reviewed"
            )
        report["anaphoric"] = outcomes([r for r in rows if int(r["case_id"][2:]) <= 40], baseline)
        report["explicit_controls"] = outcomes(
            [r for r in rows if int(r["case_id"][2:]) > 40], baseline
        )
        usage = report["usage"]
        gain = (
            1 - usage["P"]["chat_prompt_tokens"] / usage[baseline]["chat_prompt_tokens"]
            if usage[baseline]["chat_prompt_tokens"]
            else None
        )
        complete_usage = not (
            usage["P"]["chat_prompt_usage_missing"] or usage[baseline]["chat_prompt_usage_missing"]
        )
        report["input_token_reduction"] = gain
        report["input_usage_complete"] = complete_usage
        report["target"] = target(report["all_long_histories_primary"], 0.08)
        report["target"]["token_target"] = 0.25
        report["target"]["joint_point_target_met"] = (
            report["target"]["point_target_met"]
            and complete_usage
            and gain is not None
            and gain >= 0.25
        )
        successful_keys = {
            variant: {
                (r["case_id"], r["trial"])
                for r in rows
                if r["variant"] == variant and r["strict_success"]
            }
            for variant in (baseline, "P")
        }
        common = successful_keys[baseline] & successful_keys["P"]
        paired_rows = [r for r in rows if (r["case_id"], r["trial"]) in common]
        paired_usage = call_usage(paired_rows, directory) if paired_rows else {}
        report["matched_success_input_sensitivity"] = {
            "scope": "descriptive secondary analysis; both variants succeed on the same case/trial; does not replace the preregistered all-arrival metric",
            "pairs": len(common),
            "usage": paired_usage,
            "reduction": 1
            - paired_usage["P"]["chat_prompt_tokens"] / paired_usage[baseline]["chat_prompt_tokens"]
            if common and paired_usage[baseline]["chat_prompt_tokens"]
            else None,
        }
        lengths = {
            c["id"]: c["history_fixture"]["turns"]
            for c in json.loads(
                (
                    directory / "frozen-source/docs/evaluation" / manifest["dataset_filename"]
                ).read_text(encoding="utf-8")
            )["cases"]
        }
        report["history_length_descriptive"] = {
            str(length): outcomes([r for r in rows if lengths[r["case_id"]] == length], baseline)
            for length in sorted(set(lengths.values()))
        }
    else:
        dataset = json.loads(
            (directory / "frozen-source/docs/evaluation" / manifest["dataset_filename"]).read_text(
                encoding="utf-8"
            )
        )
        cases = dataset["cases"]
        recoverable, persistent, controls = recovery_groups(cases)
        report["recoverable_primary"] = outcomes(
            [r for r in rows if r["case_id"] in recoverable], baseline
        )
        report["persistent_safe_stop"] = outcomes(
            [r for r in rows if r["case_id"] in persistent], baseline
        )
        report["no_fault_controls"] = outcomes(
            [r for r in rows if r["case_id"] in controls], baseline
        )
        report["grouping"] = {
            "recoverable": sorted(recoverable),
            "persistent": sorted(persistent),
            "controls": sorted(controls),
            "basis": "frozen semantic flags, not presumed numeric ID order; initial derived grouping error preserved separately",
        }
        report["target"] = target(report["recoverable_primary"], 0.15)
        report["fault_events"] = {
            v: dict(
                Counter(
                    e["fault"] for r in rows if r["variant"] == v for e in r.get("fault_events", [])
                )
            )
            for v in (baseline, "P")
        }
        report["scoring_reference"] = (
            "External controller state changes are applied to scoring-before reference; trajectory-before is not the original pre-injection snapshot. Controller events and fixed fixtures retain original provenance."
        )
    return report


def target(result, threshold):
    comparison = result["comparison"]
    return {
        "required_difference": threshold,
        "point_target_met": comparison["family_balanced_difference"] >= threshold,
        "positive_effect_supported_95": comparison["cluster_bootstrap_95"][0] > 0,
        "target_supported_95": comparison["cluster_bootstrap_95"][0] >= threshold,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=[*EXPERIMENTS, "all"], default="all")
    parser.add_argument("--root", type=Path, default=ROOT / "artifacts/benchmarks")
    args = parser.parse_args()
    suites = EXPERIMENTS if args.suite == "all" else [args.suite]
    reports = {name: analyze(name, args.root) for name in suites}
    output = args.root / f"step10-analysis-{args.suite}.json"
    output.write_text(
        json.dumps(
            {"captured_at": datetime.now(UTC).isoformat(), "suites": reports},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {name: r.get("target", r.get("targets_by_catalog")) for name, r in reports.items()},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
