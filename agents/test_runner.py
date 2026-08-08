"""Deterministic QEMU test runner.

The Test Expert can still use an LLM to explain failures, but actual QEMU
execution should be scripted so routing and validation do not depend on whether
the model decided to call the right tools in the right order.
"""

from __future__ import annotations

import os
import re
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
    signal_match_evidence: dict | None = None,
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
        signal_match_evidence=signal_match_evidence or {},
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

    if plan.rootfs_mode != "ext4":
        return _failure_result(
            code="BLOCKED_INITRAMFS_UNSUPPORTED",
            summary="The maintenance QEMU path boots a disk image; initramfs is not supported.",
            plan=plan,
            attempts=attempt,
            steps=steps,
        )

    # The production workflow uses Test Expert's isolated declared base image
    # and persistent SSH runner.  This compatibility entry point must not
    # synthesize an initramfs/rootfs or stage legacy modules/scripts.
    if not plan.rootfs_path:
        return _failure_result(
            code="BLOCKED_BASE_IMAGE_MISSING",
            summary="A declared ext4 rootfs is required; legacy rootfs synthesis is disabled.",
            plan=plan,
            attempts=attempt,
            steps=steps,
        )
    rootfs_path = os.path.expanduser(plan.rootfs_path)
    if not os.path.exists(rootfs_path):
        return _failure_result(
            code="BLOCKED_ROOTFS_MISSING",
            summary=f"Rootfs image does not exist: {plan.rootfs_path}",
            plan=plan,
            attempts=attempt,
            steps=steps,
        )

    boot_step = boot_kernel_result(
        kernel_path=kernel_path,
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
    signal_match_evidence = _match_signal_evidence(
        log_content=log_content,
        detection=detection,
        expected_signal=expected_signal,
        start_marker=_reproduction_start_marker(plan),
    )
    matched_signal = str(signal_match_evidence.get("matched_pattern", ""))

    if matched_signal:
        causal = _check_causal_reproduction(
            log_content,
            plan,
            matched_signal,
            observed_signal=str(signal_match_evidence.get("observed_signal", "")),
        )
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
                signal_match_evidence=signal_match_evidence,
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
            signal_match_evidence=signal_match_evidence,
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
            signal_match_evidence=signal_match_evidence,
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
            signal_match_evidence=signal_match_evidence,
        )

    return _failure_result(
        code="FAILED_SIGNAL_NOT_FOUND",
        summary=_build_signal_not_found_summary(expected_signal, detection),
        plan=plan,
        attempts=attempt,
        steps=steps,
        status="failed",
        signal_match_evidence=signal_match_evidence,
    )


def _canonical_frame_symbol(frame: str) -> str:
    """Reduce a contract frame to the symbol printed by a serial trace.

    Kernel Expert evidence may include offsets, source locations, and inline
    annotations while the guest serial log usually prints only
    ``symbol+offset``.  Causal validation must compare the shared symbol, just
    like the deterministic call-chain oracle does, without treating lower
    context frames as mandatory.
    """
    value = str(frame).strip().lstrip("?* ")
    value = re.split(r"\s+", value, maxsplit=1)[0]
    return re.sub(r"\+0x[0-9a-f]+(?:/0x[0-9a-f]+)?$", "", value, flags=re.IGNORECASE)


def _frame_symbol_seen(line: str, symbol: str) -> bool:
    if not symbol:
        return False
    pattern = rf"(?<![A-Za-z0-9_.$]){re.escape(symbol)}(?![A-Za-z0-9_.$])"
    return re.search(pattern, line, flags=re.IGNORECASE) is not None


def _reproduction_start_marker(plan: TestPlan) -> str:
    """Return the causal marker used to scope serial evidence to the POC."""
    if not plan.require_causal_reproduction:
        return ""
    return f"LUMEN_REPRO_START:{plan.reproduction_case_id}:{plan.target_path_id}"


_DYNAMIC_SIGNAL_TOKEN_RE = re.compile(
    r"\+0x[0-9a-fA-F]+(?:/0x[0-9a-fA-F]+)?|0x[0-9a-fA-F]{8,}"
)


def _compile_normalized_signal(pattern: str) -> tuple[re.Pattern[str] | None, list[str]]:
    """Compile a literal signal with address/offset values normalized.

    Contracts remain literal strings.  Only hexadecimal address tokens and
    symbol offsets are made dynamic; punctuation and all other text stays
    escaped, so this cannot accidentally turn a normal signal into regex.
    """
    value = str(pattern or "")
    if not value:
        return None, []

    chunks: list[str] = []
    applied: list[str] = []
    cursor = 0
    for token in _DYNAMIC_SIGNAL_TOKEN_RE.finditer(value):
        chunks.append(re.escape(value[cursor:token.start()]))
        if token.group(0).startswith("+"):
            offset_pattern = r"\+0x[0-9a-fA-F]+"
            if "/0x" in token.group(0).lower():
                offset_pattern += r"/0x[0-9a-fA-F]+"
            chunks.append(offset_pattern + r"(?![0-9a-fA-F])")
            applied.append("symbol_offset")
        else:
            chunks.append(r"0x[0-9a-fA-F]{8,}(?![0-9a-fA-F])")
            applied.append("dynamic_address")
        cursor = token.end()
    if not applied:
        return None, []
    chunks.append(re.escape(value[cursor:]))
    return re.compile("".join(chunks), flags=re.IGNORECASE), sorted(set(applied))


def _signal_pattern_matches_text(text: str, pattern: str) -> bool:
    value = str(pattern or "").strip()
    if not value:
        return False
    if value.lower() in text.lower():
        return True
    normalized, _ = _compile_normalized_signal(value)
    return bool(normalized and normalized.search(text))


def _no_signal_evidence(*, marker_required: bool, marker_found: bool) -> dict:
    return {
        "matched": False,
        "matched_pattern": "",
        "observed_signal": "",
        "match_mode": "",
        "match_index": -1,
        "matched_after_marker": False,
        "marker_required": marker_required,
        "marker_found": marker_found,
        "normalization_applied": [],
    }


def _match_signal_evidence(
    *,
    log_content: str,
    detection: "DetectionSignals",
    expected_signal: str,
    start_marker: str = "",
) -> dict:
    """Match a serial signal and retain auditable raw/normalized evidence.

    If start_marker is supplied, pre-marker boot noise is never considered a
    match. Legacy callers omit it and retain the historical full-log scan.
    """
    lines = log_content.splitlines()
    if not lines and log_content:
        lines = [log_content]

    marker_required = bool(start_marker)
    marker_index = 0
    marker_found = not marker_required
    if start_marker:
        marker_index = next((i for i, line in enumerate(lines) if start_marker in line), -1)
        marker_found = marker_index >= 0
        if not marker_found:
            return _no_signal_evidence(marker_required=True, marker_found=False)

    scoped_lines = lines[marker_index:] if marker_required else lines
    scoped_text = "\n".join(scoped_lines)
    if not scoped_text:
        return _no_signal_evidence(
            marker_required=marker_required,
            marker_found=marker_found,
        )

    def matched(
        pattern: str,
        *,
        mode: str,
        relative_index: int,
        normalization: list[str] | None = None,
    ) -> dict:
        absolute_index = marker_index + relative_index
        return {
            "matched": True,
            "matched_pattern": pattern,
            "observed_signal": scoped_lines[relative_index],
            "match_mode": mode,
            "match_index": absolute_index,
            "matched_after_marker": (
                not marker_required or absolute_index > marker_index
            ),
            "marker_required": marker_required,
            "marker_found": marker_found,
            "normalization_applied": list(normalization or []),
        }

    # Keep the legacy first-pattern-wins ordering.
    for sig in detection.serial_signals:
        sig = sig.strip()
        if not sig:
            continue
        lower = sig.lower()
        for index, line in enumerate(scoped_lines):
            if lower in line.lower():
                return matched(sig, mode="exact", relative_index=index)
        normalized, applied = _compile_normalized_signal(sig)
        if normalized:
            match = normalized.search(scoped_text)
            if match:
                index = scoped_text[:match.start()].count("\n")
                return matched(
                    sig,
                    mode="normalized",
                    relative_index=index,
                    normalization=applied,
                )

    expected = (expected_signal or "").strip()
    if expected:
        lower = expected.lower()
        for index, line in enumerate(scoped_lines):
            if lower in line.lower():
                return matched(expected, mode="exact", relative_index=index)
        normalized, applied = _compile_normalized_signal(expected)
        if normalized:
            match = normalized.search(scoped_text)
            if match:
                index = scoped_text[:match.start()].count("\n")
                return matched(
                    expected,
                    mode="normalized",
                    relative_index=index,
                    normalization=applied,
                )

    scoped_lower = scoped_text.lower()
    if detection.panic_on_warn and "kernel panic" in scoped_lower:
        panic_index = next(
            i for i, line in enumerate(scoped_lines)
            if "kernel panic" in line.lower()
        )
        if detection.panic_is_pass:
            return matched(
                "Kernel panic (panic_on_warn=1, panic_is_pass=True)",
                mode="panic",
                relative_index=panic_index,
            )
        if _warning_precedes_panic(scoped_text):
            return matched(
                "Kernel panic (panic_on_warn=1, preceded by WARNING)",
                mode="panic",
                relative_index=panic_index,
            )

    return _no_signal_evidence(
        marker_required=marker_required,
        marker_found=marker_found,
    )


def _check_causal_reproduction(
    log_content: str,
    plan: TestPlan,
    matched_signal: str,
    *,
    observed_signal: str = "",
) -> dict:
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

    start_marker = _reproduction_start_marker(plan)
    lines = log_content.splitlines()
    start_index = next((i for i, line in enumerate(lines) if start_marker in line), -1)
    if start_index < 0:
        result["false_positive_checks"].append(f"missing reproducer marker: {start_marker}")
        return result
    result["reproducer_started"] = True

    patterns = [
        pattern for pattern in (
            list(plan.detection_signals.serial_signals)
            + [plan.expected_signal, matched_signal, observed_signal]
        ) if pattern
    ]
    if matched_signal.lower().startswith("kernel panic"):
        patterns.append("kernel panic")
    signal_index = next(
        (
            i for i in range(start_index + 1, len(lines))
            if any(_signal_pattern_matches_text(lines[i], pattern) for pattern in patterns)
        ),
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
    # Subsystem/object labels are useful annotations but often do not appear
    # verbatim in a kernel stack (for example security/smack is rendered only
    # as the individual SMACK symbols). The deterministic call-chain oracle
    # is the stronger evidence: if a required frame is present in the
    # post-signal window, use that frame as the causal context rather than
    # rejecting an otherwise exact userspace reproduction on a display-name
    # mismatch.
    if not result["matched_stack_frames"]:
        required = list(
            plan.call_chain_oracle.required_top_frames
            or plan.call_chain_oracle.required_frames
        )
        for frame in required:
            symbol = _canonical_frame_symbol(frame)
            matches = [line for line in window if _frame_symbol_seen(line, symbol)]
            if matches:
                result["matched_stack_frames"].extend(matches[:1])
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
    start_marker: str = "",
) -> str:
    """Return the first matching contract signal (legacy compatibility wrapper)."""
    evidence = _match_signal_evidence(
        log_content=log_content,
        detection=detection,
        expected_signal=expected_signal,
        start_marker=start_marker,
    )
    return str(evidence.get("matched_pattern", ""))

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
