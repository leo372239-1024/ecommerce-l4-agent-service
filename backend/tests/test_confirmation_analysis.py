"""Guard the real frozen cohort boundaries; ID ordering is not a semantic contract."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "delivery_analysis", ROOT / "scripts/analyze_step10.py"
)
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def test_registered_recovery_cohorts_follow_actual_fault_flags_not_id_ranges():
    cases = json.loads(
        (ROOT / "docs/evaluation/step10-recovery-v1.json").read_text(encoding="utf-8")
    )["cases"]
    recoverable, persistent, controls = analysis.recovery_groups(list(reversed(cases)))
    assert controls == {"RC049", "RC050"}
    assert persistent == {f"RC{i:03}" for i in range(33, 41)}
    assert {f"RC{i:03}" for i in range(51, 59)} <= recoverable
    assert len(recoverable) == 48
