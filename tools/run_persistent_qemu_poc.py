#!/usr/bin/env python3
"""Run a validated userspace-C reproduction contract in Lumen's QEMU guest.

This compatibility entry point is for Linux kernel maintenance diagnostics. It
accepts the same C-only contract consumed by Test Expert and writes a
machine-readable call-chain verdict. Kernel modules are intentionally rejected.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.contracts import (
    CallChainOracle,
    DetectionSignals,
    ExecutionStep,
    QemuRecipe,
    TestPlan,
    UserspaceReproducer,
    model_to_dict,
)
from agents.persistent_qemu import run_persistent_qemu_test_plan


def _model_validate(model, value: dict):
    if hasattr(model, "model_validate"):
        return model.model_validate(value)
    return model.parse_obj(value)


def build_plan(contract: dict) -> TestPlan:
    """Translate a Kernel Expert C-only handoff without module fallbacks."""
    if any(contract.get(key) for key in ("reproducer_module_path", "module_path")):
        raise ValueError("kernel modules are not supported; provide reproducer.source_files instead")
    reproducer = contract.get("reproducer") or {}
    if not isinstance(reproducer, dict):
        raise ValueError("reproducer must be an object describing userspace C sources")
    return TestPlan(
        target_arch=str(contract.get("target_arch", "")),
        boot_kernel_path=str(contract.get("boot_kernel_path", "")),
        rootfs_mode="ext4",
        reproducer_dir=str(reproducer.get("source_dir", contract.get("reproducer_dir", ""))),
        reproducer=_model_validate(UserspaceReproducer, reproducer),
        call_chain_oracle=_model_validate(CallChainOracle, contract.get("call_chain_oracle") or {}),
        execution_steps=[_model_validate(ExecutionStep, step) for step in (contract.get("execution_steps") or [])],
        expected_signal=str(contract.get("expected_signal", "")),
        binaries_dir=str(contract.get("binaries_dir", "")),
        detection_signals=_model_validate(DetectionSignals, contract.get("detection_signals") or {}),
        qemu_recipe=_model_validate(QemuRecipe, contract.get("qemu_recipe") or {}),
        reproduction_case_id=str(contract.get("reproduction_case_id", "")),
        target_path_id=str(contract.get("reproduction_target_path_id", "")),
        target_contexts=list((contract.get("uaf_analysis") or {}).get("target_contexts") or []),
        require_causal_reproduction=bool(contract.get("path_analysis_required")),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", required=True, help="kernel_contract.json written by the Claude loop")
    parser.add_argument("--output", required=True, help="path for deterministic persistent-QEMU result JSON")
    parser.add_argument("--attempt", type=int, default=1)
    args = parser.parse_args()
    contract_path = Path(args.contract).resolve()
    output_path = Path(args.output).resolve()
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"invalid contract file: {exc}", file=sys.stderr)
        return 2
    if not isinstance(contract, dict):
        print("kernel contract must be a JSON object", file=sys.stderr)
        return 2
    result = run_persistent_qemu_test_plan(build_plan(contract), attempt=args.attempt)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(model_to_dict(result), indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"code": result.code, "test_passed": result.test_passed, "result": str(output_path)}, ensure_ascii=False))
    return 0 if result.test_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
