"""Regression tests for P0 semantic error classification."""

from pathlib import Path
import subprocess
import sys

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from agents.error_handling import classify_error


def test_timeout_is_retryable_transient_error():
    error = classify_error(subprocess.TimeoutExpired(["tool"], 30), operation="tool step")
    assert error.category == "TRANSIENT"
    assert error.code == "OPERATION_TIMEOUT"
    assert error.retryable is True
    assert "Retry only tool step" in error.next_action


def test_missing_dependency_is_unavailable_not_a_transient_retry():
    error = classify_error(FileNotFoundError("qemu-system-aarch64 not found"), operation="QEMU")
    assert error.category == "UNAVAILABLE"
    assert error.retryable is False


if __name__ == "__main__":
    test_timeout_is_retryable_transient_error()
    test_missing_dependency_is_unavailable_not_a_transient_retry()
    print("error_handling OK")
