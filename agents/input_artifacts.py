"""Deterministic extraction of user-provided workflow artifacts."""

from __future__ import annotations

import re
import os
import json
from pathlib import Path
from typing import Any

from agents.contracts import InputArtifactsContract
from agents.test_runner import detect_kernel_type, normalize_target_arch
from paths import PROJECT_ROOT


PATH_PATTERN = r"((?:[~/]|\$)[^\s,?;?]+)"


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


def _extract_labeled_value(text: str, labels: list[str]) -> tuple[str, str]:
    """Extract a non-path value from an explicit ``label: value`` line.

    Runtime recipe values may contain several space-separated tokens, so they
    must not go through the path parser (which intentionally stops at the
    first whitespace).  Anchoring to the beginning of a line also avoids
    accidentally treating prose or JSON fragments as declarations.
    """
    for label in labels:
        pattern = rf"(?im)^\s*{re.escape(label)}\s*[：:]\s*(.+?)\s*$"
        match = re.search(pattern, text)
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
    # Inputs may be shared across developers and hosts. Expand only declared
    # environment variables and the user's home; an unresolved variable stays
    # literal and is reported as a missing artifact by validation.
    expanded = Path(os.path.expanduser(os.path.expandvars(path)))
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

    vmcore_path, vmcore_label = _extract_labeled_path(
        text,
        ["vmcore_path", "vmcore", "/proc/vmcore", "kdump"],
    )
    vmlinux_path, vmlinux_label = _extract_labeled_path(
        text,
        ["vmlinux_path", "vmlinux"],
    )
    boot_kernel_path, boot_label = _extract_labeled_path(
        text,
        [
            "boot_kernel_path",
            "boot_kernel",
            "boot kernel",
            "bzImage",
            "kernel image",
            "Image",
        ],
    )
    rootfs_path, rootfs_label = _extract_labeled_path(
        text, ["rootfs", "rootfs_path", "root filesystem", "disk image", "disk"]
    )
    qemu_extra_cmdline, qemu_extra_label = _extract_labeled_value(
        text, ["qemu_extra_cmdline", "qemu recipe extra cmdline", "extra_cmdline"]
    )
    qemu_recipe_text, qemu_recipe_label = _extract_labeled_value(
        text, ["qemu_recipe"]
    )
    qemu_recipe: dict[str, Any] = {}
    if qemu_recipe_text:
        try:
            parsed_recipe = json.loads(qemu_recipe_text)
        except json.JSONDecodeError as exc:
            errors.append(f"qemu_recipe is not valid JSON: {exc}")
        else:
            if not isinstance(parsed_recipe, dict):
                errors.append("qemu_recipe must be a JSON object")
            else:
                qemu_recipe = parsed_recipe
    expected_signal, expected_signal_label = _extract_labeled_value(
        text, ["expected_signal", "expected kernel signal", "fault signal"]
    )
    guest_sysctls_text, guest_sysctls_label = _extract_labeled_value(
        text, ["guest_sysctls", "guest_sysctl", "runtime_sysctls"]
    )
    guest_sysctls = [
        item.strip() for item in re.split(r"[,;]", guest_sysctls_text)
        if item.strip()
    ]
    kernel_source_path, source_label = _extract_labeled_path(
        text,
        [
            "kernel_source_path",
            "kernel_source",
            "kernel source",
            "linux source",
            "source tree",
        ],
    )
    log_path, log_label = _extract_labeled_path(
        text,
        ["log_path", "log", "kernel log", "dmesg"],
    )
    crash_report_path, crash_report_label = _extract_labeled_path(
        text,
        ["crash_report_path", "crash_report", "report_path", "report"],
    )
    reproducer_path, reproducer_label = _extract_labeled_path(
        text,
        [
            "reproducer_path",
            "reproducer",
            "test_script",
            "test script",
        ],
    )
    reproducer_module_path, reproducer_module_label = _extract_labeled_path(
        text,
        [
            "reproducer_module_path",
            "reproducer_module",
            "kernel_module",
        ],
    )
    reproducer_trigger_path, reproducer_trigger_label = _extract_labeled_path(
        text,
        [
            "reproducer_trigger_path",
            "reproducer_trigger",
            "trigger_source",
        ],
    )
    target_arch, arch_pattern = _extract_target_arch(text)
    expected_kernel_commit, commit_label = _extract_labeled_value(
        text, ["expected_kernel_commit", "kernel_commit", "kernel commit"]
    )
    log_excerpt = _extract_log_excerpt(text)

    fields = {
        "vmcore_path": (vmcore_path, vmcore_label),
        "vmlinux_path": (vmlinux_path, vmlinux_label),
        "boot_kernel_path": (boot_kernel_path, boot_label),
        "rootfs_path": (rootfs_path, rootfs_label),
        "qemu_extra_cmdline": (qemu_extra_cmdline, qemu_extra_label),
        "qemu_recipe": (qemu_recipe_text, qemu_recipe_label),
        "expected_signal": (expected_signal, expected_signal_label),
        "guest_sysctls": (guest_sysctls_text, guest_sysctls_label),
        "kernel_source_path": (kernel_source_path, source_label),
        "log_path": (log_path, log_label),
        "crash_report_path": (crash_report_path, crash_report_label),
        "reproducer_path": (reproducer_path, reproducer_label),
        "reproducer_module_path": (reproducer_module_path, reproducer_module_label),
        "reproducer_trigger_path": (reproducer_trigger_path, reproducer_trigger_label),
    }
    for field, (value, source) in fields.items():
        if value:
            evidence.append({"kind": "input_path", "field": field, "value": value, "source": source})
    if target_arch:
        evidence.append({"kind": "input_arch", "field": "target_arch", "value": target_arch, "source": arch_pattern})
    if expected_kernel_commit:
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", expected_kernel_commit):
            warnings.append(
                "expected_kernel_commit is not a hexadecimal git object id: "
                f"{expected_kernel_commit}"
            )
        evidence.append({
            "kind": "input_value", "field": "expected_kernel_commit",
            "value": expected_kernel_commit, "source": commit_label,
        })
    if guest_sysctls:
        evidence.append({
            "kind": "input_value", "field": "guest_sysctls",
            "value": guest_sysctls, "source": guest_sysctls_label,
        })
    if qemu_recipe:
        evidence.append({
            "kind": "input_value", "field": "qemu_recipe",
            "value": qemu_recipe, "source": qemu_recipe_label,
        })
    if log_excerpt:
        evidence.append({"kind": "input_log_excerpt", "field": "log_excerpt", "length": len(log_excerpt)})

    if vmlinux_path and not boot_kernel_path:
        warnings.append("vmlinux_path was provided without a boot_kernel_path; vmlinux is not a QEMU boot image")

    if validate_paths:
        expected_kinds = {
            "vmcore_path": "file",
            "vmlinux_path": "file",
            "boot_kernel_path": "file",
            "rootfs_path": "file",
            "kernel_source_path": "dir",
            "log_path": "file",
            "crash_report_path": "file",
            "reproducer_path": "file",
            "reproducer_module_path": "file",
            "reproducer_trigger_path": "file",
        }
        for field, (value, _) in fields.items():
            if value and field in expected_kinds:
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
        rootfs_path=rootfs_path,
        qemu_extra_cmdline=qemu_extra_cmdline,
        qemu_recipe=qemu_recipe,
        expected_signal=expected_signal,
        guest_sysctls=guest_sysctls,
        expected_kernel_commit=expected_kernel_commit,
        target_arch=target_arch,
        kernel_source_path=kernel_source_path,
        log_path=log_path,
        crash_report_path=crash_report_path,
        reproducer_path=reproducer_path,
        reproducer_module_path=reproducer_module_path,
        reproducer_trigger_path=reproducer_trigger_path,
        log_excerpt=log_excerpt,
        evidence=evidence,
        warnings=warnings,
        errors=errors,
    )
