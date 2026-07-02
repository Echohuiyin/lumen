#!/usr/bin/env python3
"""Run lightweight checks that do not require QEMU, vmcore, or pytest."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

CHECKS = [
    ["scripts/check_agent_contracts.py"],
    ["-m", "compileall", "-q", "agents", "graph", "scripts", "tests/test_agent_contracts.py"],
    ["tests/test_agent_contracts.py"],
    ["tests/test_validator_rules.py"],
    ["tests/test_pm_rules.py"],
    ["tests/test_kernel_contract.py"],
    ["tests/test_test_runner_contract.py"],
    ["tests/test_tool_evidence.py"],
    ["tests/test_semcode_mcp.py"],
]


def run_check(args: list[str]) -> int:
    command = [sys.executable, *args]
    print(f"$ {' '.join(command)}")
    completed = subprocess.run(command, cwd=PROJECT_ROOT)
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    failures: list[list[str]] = []
    for check in CHECKS:
        code = run_check(check)
        if code != 0:
            failures.append(check)

    if failures:
        print("\nFailed checks:")
        for failure in failures:
            print(f"- {' '.join(failure)}")
        return 1

    print("\nstatic_checks OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
