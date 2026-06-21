"""Deterministic QEMU test runner.

The Test Expert can still use an LLM to explain failures, but actual QEMU
execution should be scripted so routing and validation do not depend on whether
the model decided to call the right tools in the right order.
"""

from __future__ import annotations

import os
from pathlib import Path

from agents.contracts import TestPlan, TestResultContract, ToolStepResult
from agents.qemu_tools import (
    analyze_boot_log_result,
    boot_kernel_result,
    check_qemu_available_result,
    create_initramfs_result,
)


def normalize_target_arch(arch: str | None) -> str:
    value = (arch or "").strip().lower()
    aliases = {
        "x86": "x86_64",
        "x64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "arm64",
        "arm": "arm32",
        "armv7": "arm32",
        "armhf": "arm32",
    }
    return aliases.get(value, value)


def detect_kernel_type(kernel_path: str) -> str:
    """Return elf, bzimage, raw_image, or unknown."""
    try:
        with open(kernel_path, "rb") as f:
            header = f.read(4)
        if header == b"\x7fELF":
            return "elf"
        if header[:2] == b"MZ" or header == b"HdrS":
            return "bzimage"
        # ARM64 Image commonly has no bzImage setup header. Treat existing
        # non-ELF images as potentially bootable and let QEMU decide.
        return "raw_image"
    except Exception:
        return "unknown"


def _read_artifact(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _failure_result(
    *,
    code: str,
    summary: str,
    plan: TestPlan,
    attempts: int,
    steps: list[ToolStepResult],
    status: str = "blocked",
) -> TestResultContract:
    artifacts = {}
    for step in steps:
        artifacts.update(step.artifacts)
    return TestResultContract(
        status=status,
        code=code,
        test_passed=False,
        attempts=attempts,
        summary=summary,
        plan=plan,
        steps=steps,
        artifacts=artifacts,
    )


def run_qemu_test_plan(
    plan: TestPlan,
    *,
    attempt: int,
    timeout: int = 120,
    memory: str = "512M",
) -> TestResultContract:
    """Run a QEMU test plan with deterministic step order and result codes."""
    normalized_arch = normalize_target_arch(plan.target_arch)
    if hasattr(plan, "model_copy"):
        plan = plan.model_copy(update={"target_arch": normalized_arch})
    else:
        plan = plan.copy(update={"target_arch": normalized_arch})
    steps: list[ToolStepResult] = []

    if not plan.boot_kernel_path:
        return _failure_result(
            code="BLOCKED_NO_BOOT_KERNEL",
            summary="No bootable kernel image was provided.",
            plan=plan,
            attempts=attempt,
            steps=steps,
        )

    kernel_path = os.path.expanduser(plan.boot_kernel_path)
    if not os.path.exists(kernel_path):
        return _failure_result(
            code="BLOCKED_BOOT_KERNEL_MISSING",
            summary=f"Boot kernel does not exist: {plan.boot_kernel_path}",
            plan=plan,
            attempts=attempt,
            steps=steps,
        )

    kernel_type = detect_kernel_type(kernel_path)
    if kernel_type == "elf":
        return _failure_result(
            code="BLOCKED_NOT_BOOTABLE_KERNEL",
            summary=(
                "The provided kernel is an ELF vmlinux/debug-symbols file, "
                "not a bootable QEMU kernel image."
            ),
            plan=plan,
            attempts=attempt,
            steps=steps,
        )

    if not plan.target_arch:
        return _failure_result(
            code="BLOCKED_NO_TARGET_ARCH",
            summary="Target architecture is missing; refusing to guess.",
            plan=plan,
            attempts=attempt,
            steps=steps,
        )

    if plan.target_arch not in {"x86_64", "arm64", "arm32"}:
        return _failure_result(
            code="BLOCKED_UNSUPPORTED_ARCH",
            summary=f"Unsupported target architecture: {plan.target_arch}",
            plan=plan,
            attempts=attempt,
            steps=steps,
        )

    qemu_step = check_qemu_available_result(plan.target_arch)
    steps.append(qemu_step)
    if qemu_step.status != "ok":
        return _failure_result(
            code="SKIPPED_QEMU_MISSING",
            summary=f"QEMU is not available for {plan.target_arch}.",
            plan=plan,
            attempts=attempt,
            steps=steps,
            status="skipped",
        )

    modules_dir = plan.reproducer_dir
    if not modules_dir and plan.reproducer_module_path:
        modules_dir = str(Path(os.path.expanduser(plan.reproducer_module_path)).parent)

    initramfs_step = create_initramfs_result(
        arch=plan.target_arch,
        test_script_path=plan.test_script_path or None,
        modules_dir=modules_dir or None,
    )
    steps.append(initramfs_step)
    initramfs_path = initramfs_step.artifacts.get("initramfs_path", "")
    if initramfs_step.status != "ok" or not initramfs_path:
        return _failure_result(
            code="FAILED_INITRAMFS",
            summary="Failed to create initramfs for QEMU test.",
            plan=plan,
            attempts=attempt,
            steps=steps,
            status="failed",
        )

    boot_step = boot_kernel_result(
        kernel_path=kernel_path,
        initramfs_path=initramfs_path,
        arch=plan.target_arch,
        timeout=timeout,
        memory=memory,
    )
    steps.append(boot_step)

    log_path = boot_step.artifacts.get("boot_log_path", "")
    if log_path:
        patterns = [plan.expected_signal] if plan.expected_signal else None
        analyze_step = analyze_boot_log_result(log_path=log_path, patterns=patterns)
        steps.append(analyze_step)

    artifacts = {}
    for step in steps:
        artifacts.update(step.artifacts)

    log_content = _read_artifact(log_path) if log_path else ""
    expected_signal = plan.expected_signal.strip()
    if not expected_signal:
        return TestResultContract(
            status="inconclusive",
            code="INCONCLUSIVE_NO_EXPECTED_SIGNAL",
            test_passed=False,
            attempts=attempt,
            summary="QEMU ran, but no expected signal was provided to prove reproduction.",
            plan=plan,
            steps=steps,
            artifacts=artifacts,
        )

    if expected_signal.lower() in log_content.lower():
        return TestResultContract(
            status="ok",
            code="PASSED_REPRODUCED",
            test_passed=True,
            attempts=attempt,
            summary=f"Expected signal was found in QEMU boot log: {expected_signal}",
            plan=plan,
            steps=steps,
            artifacts=artifacts,
        )

    if boot_step.status != "ok":
        code = "FAILED_TIMEOUT" if "timed out" in boot_step.message.lower() else "FAILED_BOOT"
        return _failure_result(
            code=code,
            summary="QEMU boot did not complete and expected signal was not found.",
            plan=plan,
            attempts=attempt,
            steps=steps,
            status="failed",
        )

    return _failure_result(
        code="FAILED_SIGNAL_NOT_FOUND",
        summary=f"QEMU completed, but expected signal was not found: {expected_signal}",
        plan=plan,
        attempts=attempt,
        steps=steps,
        status="failed",
    )
