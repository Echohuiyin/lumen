"""Structured contracts shared by maintenance workflow agents.

The workflow still keeps human-readable text for prompts and reports, but these
models provide machine-readable status, failure reasons, and artifacts for
routing and tests.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


StepStatus = Literal["ok", "failed", "blocked", "skipped"]
WorkflowStatus = Literal["ok", "failed", "blocked", "skipped", "degraded", "inconclusive"]


def model_to_dict(model: BaseModel) -> dict[str, Any]:
    """Return a dict for both Pydantic v1 and v2 runtimes."""
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


class DetectionSignals(BaseModel):
    """How Test Expert should grep the host-side serial log to decide PASS/FAIL.

    The host-side serial log is the ground truth — guest test.sh may never run
    because panic_on_warn=1 escalates WARNINGs to panic + reboot before the
    script's `dmesg | grep` fires. So detection must happen on the host side,
    against the serial log file written by QEMU's -serial file: option.

    Fields:
      serial_signals: Ordered list of patterns to search (substring match, case-
        insensitive). First match wins — order from most-specific (e.g.
        "pvqspinlock: lock.*corrupted value") to most-generic ("Kernel panic").
        Listed *together* because matching any one is sufficient evidence.
      panic_on_warn: True if the kernel was booted with panic_on_warn=1 (or the
        equivalent is embedded in CONFIG_CMDLINE). When True, a `Kernel panic`
        line preceded by a WARNING within ~100 lines is itself a PASS signal —
        the panic is the WARNING being escalated, not a spurious crash.
      panic_is_pass: When True, `Kernel panic` lines count as PASS even without
        an explicit WARNING prefix. Use for reproducer types that escalate
        directly to panic (e.g. panic_on_warn=1 + any WARNING, BUG, or oops).
        When False, `Kernel panic` alone is NOT a pass — it might be a boot
        failure unrelated to the target bug.
    """

    serial_signals: list[str] = Field(default_factory=list)
    panic_on_warn: bool = False
    panic_is_pass: bool = False


class QemuRecipe(BaseModel):
    """QEMU launch configuration declared by kernel_expert, consumed by
    qemu_tools.boot_kernel(). Replaces the hardcoded smp_spec="2" / i440FX /
    memory auto-select that was scattered across qemu_tools.py.

    Every field has a sentinel/empty default so an empty QemuRecipe() means
    "fall back to existing qemu_tools defaults" — backward compatible with
    contracts that predate this field.

    Fields:
      machine: QEMU -machine string, e.g. "q35,accel=kvm:tcg" or
        "accel=kvm:tcg" (i440FX default). Empty → "accel=kvm:tcg".
      cpu: QEMU -cpu string, e.g. "host" or "qemu64". Empty → "host".
      smp: Number of vCPUs for the L1 guest, e.g. "4". Empty → "2" (legacy
        default). Race-condition bugs often need smp=4 or higher to create
        vCPU thread overcommitment.
      memory: QEMU -m string, e.g. "2G" or "4G". Empty → auto-select by
        kernel size (KASAN kernels get 2G, others get 512M).
      extra_cmdline: Extra kernel command-line args appended to the default
        "console=ttyS0 root=/dev/ram rw panic=1 oops=panic ...". Use for
        bug-specific flags like "panic_on_warn=1" (WARNING escalation),
        "numa=off" (avoid set_cpu_sibling_map warning), or
        "kvm-intel.nested=1" (nested virt for KVM bugs).
      concurrent_instances: How many parallel QEMU VMs to launch with the same
        recipe — useful for race-condition bugs that need vCPU thread
        overcommitment across multiple VMs. 1 = single VM (default). The
        test_expert runner loops this many times and merges serial logs.
      timeout_sec: QEMU boot timeout. 0/empty → use runner default (900s).
    """

    machine: str = ""
    cpu: str = ""
    smp: str = ""
    memory: str = ""
    extra_cmdline: str = ""
    concurrent_instances: int = 1
    timeout_sec: int = 0


class ToolStepResult(BaseModel):
    """One deterministic tool/script step with enough data for debugging."""

    name: str
    status: StepStatus
    message: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    output: str = ""
    error: str = ""


class TestPlan(BaseModel):
    """Machine-readable handoff from Kernel Expert to Test Expert."""

    target_arch: str = ""
    boot_kernel_path: str = ""
    rootfs_mode: Literal["initramfs", "ext4"] = "initramfs"
    rootfs_path: str = ""
    rootfs_size_mb: int = 128
    reproducer_dir: str = ""
    reproducer_module_path: str = ""
    test_script_path: str = ""
    expected_signal: str = ""
    binaries_dir: str = ""
    detection_signals: DetectionSignals = Field(default_factory=DetectionSignals)
    qemu_recipe: QemuRecipe = Field(default_factory=QemuRecipe)


class TestResultContract(BaseModel):
    """Deterministic test result consumed by routers, validators, and KB."""

    status: WorkflowStatus
    code: str
    test_passed: bool = False
    attempts: int = 0
    summary: str = ""
    plan: TestPlan = Field(default_factory=TestPlan)
    steps: list[ToolStepResult] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)


class ValidationResultContract(BaseModel):
    """Structured input validation result."""

    status: WorkflowStatus
    validation_passed: bool = False
    reason: str = ""
    missing_fields: list[str] = Field(default_factory=list)
    detected_signals: list[str] = Field(default_factory=list)
    feedback: str = ""


class InputArtifactsContract(BaseModel):
    """Deterministically extracted artifacts from user input."""

    status: WorkflowStatus = "inconclusive"
    vmcore_path: str = ""
    vmlinux_path: str = ""
    boot_kernel_path: str = ""
    target_arch: str = ""
    kernel_source_path: str = ""
    reproducer_path: str = ""
    log_excerpt: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class KernelExpertOutput(BaseModel):
    """Structured Kernel Expert output.

    This is introduced as a forward-compatible contract. Current code can still
    use marker parsing as a fallback until the prompt is fully JSON-first.
    """

    status: WorkflowStatus = "degraded"
    target_arch: str = ""
    vmlinux_path: str = ""
    boot_kernel_path: str = ""
    rootfs_mode: Literal["initramfs", "ext4"] = "ext4"
    rootfs_path: str = ""
    rootfs_size_mb: int = 128
    reproducer_dir: str = ""
    reproducer_module_path: str = ""
    test_script_path: str = ""
    expected_signal: str = ""
    binaries_dir: str = ""
    build_status: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blocked_reason: str = ""
    detection_signals: DetectionSignals = Field(default_factory=DetectionSignals)
    qemu_recipe: QemuRecipe = Field(default_factory=QemuRecipe)


class ToolExpertOutput(BaseModel):
    """Structured evidence wrapper for Crash/Lock/Log/Knowledge experts."""

    expert_type: str
    expert_name: str = ""
    status: WorkflowStatus = "degraded"
    summary: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
