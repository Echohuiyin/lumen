"""Deterministic QEMU test runner.

The Test Expert can still use an LLM to explain failures, but actual QEMU
execution should be scripted so routing and validation do not depend on whether
the model decided to call the right tools in the right order.
"""

from __future__ import annotations

import os
from pathlib import Path

from agents.contracts import (
    DetectionSignals,
    TestPlan,
    TestResultContract,
    ToolStepResult,
)
from agents.qemu_tools import (
    _DEFAULT_BOOT_ERROR_PATTERNS,
    _select_qemu_memory,
    analyze_boot_log_result,
    boot_kernel_result,
    check_qemu_available_result,
    create_ext4_rootfs_result,
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
    timeout: int = 900,
    memory: str = "",
) -> TestResultContract:
    """Run a QEMU test plan with deterministic step order and result codes.

    Memory is auto-selected from kernel size when not specified — KASAN
    kernels need >=2GB or they panic during kasan_populate_shadow.
    """
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

    # Auto-select memory based on kernel size (KASAN kernels need >=2GB)
    if not memory:
        memory = _select_qemu_memory(kernel_path, "")

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

    initramfs_path = ""
    rootfs_path = ""
    if plan.rootfs_mode == "ext4":
        if plan.rootfs_path:
            rootfs_path = os.path.expanduser(plan.rootfs_path)
            if not os.path.exists(rootfs_path):
                return _failure_result(
                    code="BLOCKED_ROOTFS_MISSING",
                    summary=f"Rootfs image does not exist: {plan.rootfs_path}",
                    plan=plan,
                    attempts=attempt,
                    steps=steps,
                )
        else:
            rootfs_step = create_ext4_rootfs_result(
                arch=plan.target_arch,
                test_script_path=None,
                modules_dir=modules_dir or None,
                binaries_dir=plan.binaries_dir or None,
                size_mb=plan.rootfs_size_mb or 128,
            )
            steps.append(rootfs_step)
            rootfs_path = rootfs_step.artifacts.get("rootfs_path", "")
            if rootfs_step.status != "ok" or not rootfs_path:
                return _failure_result(
                    code="FAILED_EXT4_ROOTFS",
                    summary="Failed to create ext4 rootfs for QEMU test.",
                    plan=plan,
                    attempts=attempt,
                    steps=steps,
                    status="failed",
                )
    else:
        initramfs_step = create_initramfs_result(
            arch=plan.target_arch,
            test_script_path=None,
            modules_dir=modules_dir or None,
            binaries_dir=plan.binaries_dir or None,
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
        rootfs_path=rootfs_path,
        arch=plan.target_arch,
        timeout=plan.qemu_recipe.timeout_sec if plan.qemu_recipe.timeout_sec else timeout,
        memory=plan.qemu_recipe.memory or memory,
        qemu_recipe=plan.qemu_recipe,
    )
    steps.append(boot_step)

    log_path = boot_step.artifacts.get("boot_log_path", "")
    if log_path:
        # Always scan for the default kernel-error patterns so boot-time
        # crashes (KASAN panic, NULL deref, etc.) are surfaced even when
        # the expected_signal targets a different fault type. Otherwise a
        # boot that dies before the reproducer runs looks like a plain
        # timeout with "no patterns found".
        patterns = list(_DEFAULT_BOOT_ERROR_PATTERNS)
        if plan.expected_signal and plan.expected_signal not in patterns:
            patterns.append(plan.expected_signal)
        analyze_step = analyze_boot_log_result(log_path=log_path, patterns=patterns)
        steps.append(analyze_step)

    artifacts = {}
    for step in steps:
        artifacts.update(step.artifacts)

    log_content = _read_artifact(log_path) if log_path else ""
    expected_signal = plan.expected_signal.strip()

    # Detection signals: kernel_expert can declare a structured list of
    # patterns to grep on the host-side serial log. The serial log is the
    # ground truth — guest test.sh's `dmesg | grep` never fires when
    # panic_on_warn=1 escalates WARNING → panic → reboot before the script
    # gets a chance to check.
    detection = plan.detection_signals
    matched_signal = _match_serial_signals(
        log_content=log_content,
        detection=detection,
        expected_signal=expected_signal,
    )

    if matched_signal:
        causal = _check_causal_reproduction(log_content, plan, matched_signal)
        if plan.require_causal_reproduction and not (
            causal["reproducer_started"]
            and causal["signal_after_start"]
            and causal["target_context_matched"]
        ):
            return TestResultContract(
                status="failed",
                code="FAILED_CAUSAL_REPRODUCTION",
                test_passed=False,
                attempts=attempt,
                summary="A matching signal was present, but causal reproduction proof was incomplete.",
                plan=plan,
                steps=steps,
                artifacts=artifacts,
                target_path_id=plan.target_path_id,
                **causal,
            )
        return TestResultContract(
            status="ok",
            code="PASSED_REPRODUCED",
            test_passed=True,
            attempts=attempt,
            summary=f"Expected signal was found in QEMU boot log: {matched_signal}",
            plan=plan,
            steps=steps,
            artifacts=artifacts,
            target_path_id=plan.target_path_id,
            **causal,
        )

    if not expected_signal and not detection.serial_signals:
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
        summary=_build_signal_not_found_summary(expected_signal, detection),
        plan=plan,
        attempts=attempt,
        steps=steps,
        status="failed",
    )


def _check_causal_reproduction(log_content: str, plan: TestPlan, matched_signal: str) -> dict:
    """Verify the signal belongs to the selected reproducer, not boot noise."""
    result = {
        "reproducer_started": False,
        "signal_after_start": False,
        "target_context_matched": False,
        "matched_stack_frames": [],
        "false_positive_checks": [],
    }
    if not plan.require_causal_reproduction:
        return result

    start_marker = f"LUMEN_REPRO_START:{plan.reproduction_case_id}:{plan.target_path_id}"
    lines = log_content.splitlines()
    start_index = next((i for i, line in enumerate(lines) if start_marker in line), -1)
    if start_index < 0:
        result["false_positive_checks"].append(f"missing reproducer marker: {start_marker}")
        return result
    result["reproducer_started"] = True

    patterns = [pattern for pattern in (
        list(plan.detection_signals.serial_signals) + [plan.expected_signal, matched_signal]
    ) if pattern]
    signal_index = next(
        (i for i in range(start_index + 1, len(lines))
         if any(pattern.lower() in lines[i].lower() for pattern in patterns)),
        -1,
    )
    if signal_index < 0:
        result["false_positive_checks"].append("target signal was only observed before reproducer start")
        return result
    result["signal_after_start"] = True

    window = lines[start_index:signal_index + 81]
    for context in plan.target_contexts:
        matches = [line for line in window if context.lower() in line.lower()]
        if matches:
            result["matched_stack_frames"].extend(matches[:3])
    result["target_context_matched"] = bool(result["matched_stack_frames"])
    if not result["target_context_matched"]:
        result["false_positive_checks"].append(
            "no target module/function/object context matched after reproducer start"
        )
    return result


def _match_serial_signals(
    *,
    log_content: str,
    detection: "DetectionSignals",
    expected_signal: str,
) -> str:
    """Return the first matching signal pattern, or empty string if none match.

    Detection order (first match wins):
      1. detection.serial_signals — structured patterns declared by kernel_expert.
         Searched in order; most-specific first.
      2. expected_signal — legacy single-pattern field (substring match).
      3. panic_on_warn fallback — if kernel was booted with panic_on_warn=1
         and a `Kernel panic` line appears in the log, treat as PASS *only if*
         a WARNING/Oops/BUG line appears within the preceding ~100 lines
         (i.e. the panic is the escalation of a real warning, not a boot crash).
         If panic_is_pass is True, treat any `Kernel panic` as PASS without
         the WARNING proximity requirement.

    All matching is case-insensitive substring (NOT regex) — the `.*` in
    pattern literals will be treated as characters. kernel_expert should
    emit short literal substrings (e.g. "pvqspinlock: lock" not
    "pvqspinlock: lock.*corrupted value") for reliable matching.
    """
    log_lower = log_content.lower()
    if not log_lower:
        return ""

    for sig in detection.serial_signals:
        sig = sig.strip()
        if sig and sig.lower() in log_lower:
            return sig

    if expected_signal and expected_signal.lower() in log_lower:
        return expected_signal

    if detection.panic_on_warn and "kernel panic" in log_lower:
        if detection.panic_is_pass:
            return "Kernel panic (panic_on_warn=1, panic_is_pass=True)"
        # Check if a WARNING/Oops/BUG precedes the panic within ~100 lines.
        if _warning_precedes_panic(log_content):
            return "Kernel panic (panic_on_warn=1, preceded by WARNING)"

    return ""


def _warning_precedes_panic(log_content: str) -> bool:
    """True if a WARNING/Oops/BUG line appears within 100 lines before a panic.

    Used to distinguish panic_on_warn escalation (real WARNING → panic, should
    count as PASS) from spurious boot-time panics (e.g. kasan_populate_shadow
    OOM, missing rootfs — those are NOT the target bug).
    """
    lines = log_content.splitlines()
    warning_tokens = ("warning:", "oops:", "bug:", "kernel bug at", "---[ cut here ]---")
    panic_idx = -1
    for i, line in enumerate(lines):
        if "kernel panic" in line.lower():
            panic_idx = i
            break
    if panic_idx < 0:
        return False
    start = max(0, panic_idx - 100)
    for line in lines[start:panic_idx]:
        low = line.lower()
        if any(tok in low for tok in warning_tokens):
            return True
    return False


def _build_signal_not_found_summary(
    expected_signal: str,
    detection: "DetectionSignals",
) -> str:
    parts = []
    if expected_signal:
        parts.append(f"expected_signal not found: {expected_signal}")
    if detection.serial_signals:
        parts.append(f"detection.serial_signals not found: {detection.serial_signals}")
    if detection.panic_on_warn:
        parts.append("panic_on_warn=1 but no WARNING-precedes-panic pattern matched")
    return "; ".join(parts) or "QEMU completed, but no expected signal was found."
