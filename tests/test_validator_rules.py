"""Rule-first validator tests."""

from pathlib import Path
import sys

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from agents.validator import _validate_input_by_rules, validator_node


def test_empty_input_blocks():
    result = _validate_input_by_rules("")
    assert result.status == "blocked"
    assert result.validation_passed is False
    assert "problem_description" in result.missing_fields


def test_kernel_panic_passes_without_llm():
    result = _validate_input_by_rules(
        "内核 panic，vmcore: /tmp/vmcore，vmlinux: /tmp/vmlinux，Call Trace 显示 NULL pointer"
    )
    assert result.status == "ok"
    assert result.validation_passed is True
    assert "crash" in result.detected_signals


def test_deadlock_passes_without_llm():
    result = _validate_input_by_rules(
        "系统 hung task blocked for more than 120 seconds，怀疑 mutex deadlock"
    )
    assert result.status == "ok"
    assert result.validation_passed is True
    assert "lock" in result.detected_signals


def test_vague_input_blocks():
    result = _validate_input_by_rules("有问题，帮我看看")
    assert result.status == "blocked"
    assert result.validation_passed is False
    assert "problem_symptom" in result.missing_fields


def test_validator_node_returns_contract_for_rule_pass():
    state = {
        "user_input": "kernel panic with vmcore and vmlinux",
        "config_path": "config.json",
    }
    result = validator_node(state)
    assert result["validation_passed"] is True
    assert result["validation_contract"]["reason"] == "rule_detected_kernel_signals"
    assert result["config"]


if __name__ == "__main__":
    for test in [
        test_empty_input_blocks,
        test_kernel_panic_passes_without_llm,
        test_deadlock_passes_without_llm,
        test_vague_input_blocks,
        test_validator_node_returns_contract_for_rule_pass,
    ]:
        test()
    print("validator_rules OK")
