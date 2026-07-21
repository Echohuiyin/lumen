#!/usr/bin/env python3
"""Run a validated kernel contract in Lumen's persistent SSH QEMU guest.

This is the only supported POC execution entry point for the Claude loop.  It
writes a machine-readable result that the workflow later consumes as evidence;
the model cannot turn a claimed result into a passing verdict by prose alone.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.contracts import DetectionSignals, ExecutionStep, QemuRecipe, TestPlan, model_to_dict
from agents.persistent_qemu import run_persistent_qemu_test_plan


def _model_validate(model, value: dict):
    if hasattr(model, "model_validate"):
        return model.model_validate(value)
    return model.parse_obj(value)


def build_plan(contract: dict) -> TestPlan:
    """Translate the explicit kernel contract without defaulting missing fields."""
    return TestPlan(
        target_arch=str(contract.get("target_arch", "")),
        boot_kernel_path=str(contract.get("boot_kernel_path", "")),
        rootfs_mode="ext4",
        reproducer_dir=str(contract.get("reproducer_dir", "")),
        reproducer_module_path=str(contract.get("reproducer_module_path", "")),
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
