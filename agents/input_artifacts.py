"""Deterministic extraction of user-provided workflow artifacts."""

from __future__ import annotations

import re
import os
from pathlib import Path

from agents.contracts import InputArtifactsContract
from agents.test_runner import detect_kernel_type, normalize_target_arch
from paths import PROJECT_ROOT


PATH_PATTERN = r"([~/][^\s,，;；]+)"


def _extract_labeled_path(text: str, labels: list[str]) -> tuple[str, str]:
    for label in labels:
        label_pattern = re.escape(label).replace(r"\ ", r"\s+")
        patterns = [
            rf"{label_pattern}\s*(?:文件|file|path|路径)?\s*[：:]\s*{PATH_PATTERN}",
            rf"{label_pattern}\s+{PATH_PATTERN}",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip().strip("`'\""), label
    return "", ""


def _extract_target_arch(text: str) -> tuple[str, str]:
    lowered = text.lower()
    patterns = [
        ("arm64", r"\b(aarch64|arm64)\b"),
        ("arm32", r"\b(arm32|armv7|armhf)\b"),
        ("x86_64", r"\b(x86_64|amd64|x64)\b"),
    ]
    for arch, pattern in patterns:
        if re.search(pattern, lowered):
            return normalize_target_arch(arch), pattern
    return "", ""


def _extract_log_excerpt(text: str, limit: int = 4000) -> str:
    log_markers = [
        "kernel panic",
        "call trace",
        "oops",
        "blocked for more than",
        "soft lockup",
        "hard lockup",
        "unable to handle",
        "BUG:",
    ]
    lowered = text.lower()
    first = -1
    for marker in log_markers:
        idx = lowered.find(marker.lower())
        if idx >= 0 and (first < 0 or idx < first):
            first = idx
    if first < 0:
        return ""
    start = max(0, first - 500)
    return text[start:start + limit].strip()


def _resolve_input_path(path: str) -> Path:
    expanded = Path(os.path.expanduser(path))
    if not expanded.is_absolute():
        expanded = PROJECT_ROOT / expanded
    return expanded.resolve()


def _validate_path_artifact(
    *,
    field: str,
    raw_path: str,
    expected_kind: str,
    evidence: list[dict],
    warnings: list[str],
    errors: list[str],
) -> None:
    resolved = _resolve_input_path(raw_path)
    if not resolved.exists():
        warnings.append(f"{field} does not exist: {raw_path}")
        evidence.append({
            "kind": "input_artifact_check",
            "field": field,
            "path": str(resolved),
            "exists": False,
            "expected_kind": expected_kind,
        })
        return

    is_dir = resolved.is_dir()
    is_file = resolved.is_file()
    if expected_kind == "dir" and not is_dir:
        errors.append(f"{field} is not a directory: {raw_path}")
    elif expected_kind == "file" and not is_file:
        errors.append(f"{field} is not a file: {raw_path}")

    check = {
        "kind": "input_artifact_check",
        "field": field,
        "path": str(resolved),
        "exists": True,
        "expected_kind": expected_kind,
        "is_file": is_file,
        "is_dir": is_dir,
    }
    if field in {"boot_kernel_path", "vmlinux_path"} and is_file:
        kernel_type = detect_kernel_type(str(resolved))
        check["kernel_type"] = kernel_type
        if field == "boot_kernel_path" and kernel_type == "elf":
            errors.append("boot_kernel_path points to ELF vmlinux/debug symbols, not a bootable kernel image")
        if field == "vmlinux_path" and kernel_type != "elf":
            warnings.append(f"vmlinux_path does not look like an ELF debug-symbol image: {raw_path}")
    if field == "kernel_source_path" and is_dir:
        markers = ["Makefile", "Kconfig", "include/linux/kernel.h", "init/main.c"]
        missing = [marker for marker in markers if not (resolved / marker).exists()]
        check["linux_source_markers"] = markers
        check["missing_linux_source_markers"] = missing
        check["is_linux_source_tree"] = not missing
        if missing:
            warnings.append(
                "kernel_source_path does not look like a Linux source tree; "
                f"missing: {', '.join(missing)}"
            )
    evidence.append(check)


def parse_input_artifacts(user_input: str, *, validate_paths: bool = True) -> InputArtifactsContract:
    """Parse common artifact paths and target metadata from free-form input."""
    text = user_input or ""
    evidence: list[dict] = []
    warnings: list[str] = []
    errors: list[str] = []

    vmcore_path, vmcore_label = _extract_labeled_path(text, ["vmcore", "/proc/vmcore", "kdump"])
    vmlinux_path, vmlinux_label = _extract_labeled_path(text, ["vmlinux"])
    boot_kernel_path, boot_label = _extract_labeled_path(
        text,
        ["boot_kernel", "boot kernel", "bzImage", "kernel image", "Image"],
    )
    kernel_source_path, source_label = _extract_labeled_path(
        text,
        ["kernel_source", "kernel source", "linux source", "source tree"],
    )
    log_path, log_label = _extract_labeled_path(text, ["log", "kernel log", "dmesg"])
    reproducer_path, reproducer_label = _extract_labeled_path(
        text,
        ["reproducer", "reproducer_path", "test_script", "test script"],
    )
    target_arch, arch_pattern = _extract_target_arch(text)
    log_excerpt = _extract_log_excerpt(text)

    fields = {
        "vmcore_path": (vmcore_path, vmcore_label),
        "vmlinux_path": (vmlinux_path, vmlinux_label),
        "boot_kernel_path": (boot_kernel_path, boot_label),
        "kernel_source_path": (kernel_source_path, source_label),
        "log_path": (log_path, log_label),
        "reproducer_path": (reproducer_path, reproducer_label),
    }
    for field, (value, source) in fields.items():
        if value:
            evidence.append({"kind": "input_path", "field": field, "value": value, "source": source})
    if target_arch:
        evidence.append({"kind": "input_arch", "field": "target_arch", "value": target_arch, "source": arch_pattern})
    if log_excerpt:
        evidence.append({"kind": "input_log_excerpt", "field": "log_excerpt", "length": len(log_excerpt)})

    if vmlinux_path and not boot_kernel_path:
        warnings.append("vmlinux_path was provided without a boot_kernel_path; vmlinux is not a QEMU boot image")

    if validate_paths:
        expected_kinds = {
            "vmcore_path": "file",
            "vmlinux_path": "file",
            "boot_kernel_path": "file",
            "kernel_source_path": "dir",
            "log_path": "file",
            "reproducer_path": "file",
        }
        for field, (value, _) in fields.items():
            if value:
                _validate_path_artifact(
                    field=field,
                    raw_path=value,
                    expected_kind=expected_kinds[field],
                    evidence=evidence,
                    warnings=warnings,
                    errors=errors,
                )

    if errors:
        status = "degraded"
    elif warnings:
        status = "degraded"
    else:
        status = "ok" if evidence else "inconclusive"
    return InputArtifactsContract(
        status=status,
        vmcore_path=vmcore_path,
        vmlinux_path=vmlinux_path,
        boot_kernel_path=boot_kernel_path,
        target_arch=target_arch,
        kernel_source_path=kernel_source_path,
        log_path=log_path,
        reproducer_path=reproducer_path,
        log_excerpt=log_excerpt,
        evidence=evidence,
        warnings=warnings,
        errors=errors,
    )
