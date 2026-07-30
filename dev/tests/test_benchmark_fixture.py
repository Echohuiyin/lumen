"""Offline contract checks for the benchmark-v0.2 E2E input fixture.

The fixture intentionally contains only report-time observations.  It is used
to verify that the maintenance workflow can consume a stable set of kernel
problem reports without putting Gold labels or post-hoc repair evidence into
the model-facing input.
"""

from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = PROJECT_ROOT / "test_assets" / "benchmark_v0.2" / "benchmark_inputs_v0.2.jsonl"

EXPECTED_ISSUES = {
    "044fdf24e96093584232",
    "07bb74aeafc88ba7d5b4",
    "0a89a7b56db04c21a656",
    "56edda805363e0a093b8",
    "5c04210f7c7f897c1e7f",
    "b825d87fe2d043e3e652",
    "e24baf53dc389927a7c3",
    "eaaaf38a95427be88f4b",
}

REQUIRED_FIELDS = {
    "dataset_version",
    "issue_id",
    "title",
    "subsystem",
    "as_of",
    "report_evidence",
    "bug_type",
    "sanitizer",
    "concurrency_class",
    "repro_artifact_level",
    "reproduction_probability",
    "probability_source",
    "conditions",
    "condition_complexity_score",
}

REPORT_EVIDENCE_FIELDS = {
    "observed_at",
    "crash_summary",
    "top_stack",
    "execution_context",
    "repro_at_observation",
    "environment_notes",
}

# These fields are Gold, selection, or post-hoc evidence and must never appear
# in a model-facing benchmark input record.
FORBIDDEN_LEAK_FIELDS = {
    "label",
    "selection",
    "fix_commit",
    "fixed_at",
    "final_status",
    "source_url",
    "adjudication",
    "posthoc",
}


def _records() -> list[dict]:
    assert FIXTURE.is_file(), f"missing benchmark fixture: {FIXTURE}"
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def _walk_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(value)
        for child in value.values():
            keys.update(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_walk_keys(child))
    return keys


def test_benchmark_fixture_is_complete_and_unique():
    records = _records()
    assert len(records) == len(EXPECTED_ISSUES) == 8
    assert {record["issue_id"] for record in records} == EXPECTED_ISSUES
    assert len({record["issue_id"] for record in records}) == len(records)

    for record in records:
        assert REQUIRED_FIELDS <= set(record)
        evidence = record["report_evidence"]
        assert REPORT_EVIDENCE_FIELDS <= set(evidence)
        assert evidence["observed_at"] == record["as_of"]
        assert isinstance(evidence["top_stack"], list) and evidence["top_stack"]
        assert isinstance(record["conditions"], dict)
        assert 0 <= record["condition_complexity_score"] <= 5


def test_benchmark_fixture_contains_no_gold_or_posthoc_answers():
    for record in _records():
        leaked = _walk_keys(record) & FORBIDDEN_LEAK_FIELDS
        assert not leaked, f"model-facing fixture leaks forbidden fields: {sorted(leaked)}"
        assert record["reproduction_probability"] is None
        assert record["probability_source"] == "not_measured"


def test_benchmark_fixture_is_report_time_only():
    for record in _records():
        assert record["dataset_version"] == "0.2"
        assert record["as_of"] == record["report_evidence"]["observed_at"]
        assert set(record["conditions"]) == {
            "requires_disk_image",
            "requires_corrupt_fs",
            "requires_special_protocol",
            "requires_special_kernel_config",
            "requires_concurrency_stress",
            "requires_special_hardware",
            "requires_multi_stage_sequence",
            "requires_timing_control",
            "requires_reset_between_trials",
        }


if __name__ == "__main__":
    for test in (
        test_benchmark_fixture_is_complete_and_unique,
        test_benchmark_fixture_contains_no_gold_or_posthoc_answers,
        test_benchmark_fixture_is_report_time_only,
    ):
        test()
    print("test_benchmark_fixture OK")
