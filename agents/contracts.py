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
ErrorCategory = Literal[
    "TRANSIENT", "VALID_EMPTY", "PARTIAL", "INVALID_INPUT", "UNAVAILABLE",
    "PERMANENT", "INTERNAL_BUG",
]


def model_to_dict(model: BaseModel) -> dict[str, Any]:
    """Return a dict for both Pydantic v1 and v2 runtimes."""
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


class DetectionSignals(BaseModel):
    """How the persistent QEMU runner greps the host-side serial log to decide PASS/FAIL.

    The host-side serial log is the ground truth — guest execution may never
    complete because panic_on_warn=1 escalates WARNINGs to panic + reboot
    before the requested steps finish. So detection must happen on the host side,
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
        persistent runner loops this many times and merges serial logs.
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


class ExecutionStep(BaseModel):
    """One allow-listed action for deterministic guest-side reproduction.

    The Kernel Expert selects these actions from source/log evidence.  The
    persistent runner validates and executes them in order; it never infers a
    module load or evaluates an agent-authored shell script.
    """

    type: Literal["load_module", "run_binary", "run_pressure", "write_sysctl", "wait"]
    path: str = ""
    args: list[str] = Field(default_factory=list)
    key: str = ""
    value: str = ""
    seconds: int = 0
    profile: Literal["cpu", "memory", "io", "scheduler", "filesystem"] = "cpu"
    workers: int = 1


class ErrorEnvelope(BaseModel):
    """Actionable, stable error data for node and external-tool failures."""

    category: ErrorCategory
    code: str
    message: str
    retryable: bool = False
    next_action: str = ""
    cause: str = ""


class TestPlan(BaseModel):
    """Machine-readable plan executed by the persistent QEMU runner."""

    target_arch: str = ""
    boot_kernel_path: str = ""
    rootfs_mode: Literal["initramfs", "ext4"] = "initramfs"
    rootfs_path: str = ""
    rootfs_size_mb: int = 128
    reproducer_dir: str = ""
    reproducer_module_path: str = ""
    execution_steps: list[ExecutionStep] = Field(default_factory=list)
    expected_signal: str = ""
    binaries_dir: str = ""
    detection_signals: DetectionSignals = Field(default_factory=DetectionSignals)
    qemu_recipe: QemuRecipe = Field(default_factory=QemuRecipe)
    reproduction_case_id: str = ""
    target_path_id: str = ""
    target_contexts: list[str] = Field(default_factory=list)
    require_causal_reproduction: bool = False


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
    target_path_id: str = ""
    reproducer_started: bool = False
    signal_after_start: bool = False
    target_context_matched: bool = False
    matched_stack_frames: list[str] = Field(default_factory=list)
    false_positive_checks: list[str] = Field(default_factory=list)


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
    log_path: str = ""
    reproducer_path: str = ""
    log_excerpt: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class PathAnalysisScope(BaseModel):
    """Boundaries of a UAF/refcount path investigation.

    This is deliberately a small P0 contract rather than a full call-graph
    model.  It makes the assumptions behind a path inventory explicit, so an
    archive cannot accidentally present a partial search as exhaustive.
    """

    kernel_commit: str = ""
    kernel_config: str = ""
    entry_points: list[str] = Field(default_factory=list)
    object_type: str = ""
    concurrency_model: str = ""
    analysis_status: Literal["applicable", "blocked", "not_applicable", "source_unavailable"] = "applicable"
    source_domains: list[dict[str, Any]] = Field(default_factory=list)


class ExcludedPath(BaseModel):
    """A considered path which was excluded, together with its evidence."""

    path: str
    rationale: str


class ReferenceEvent(BaseModel):
    """One get/put/transfer/free/access event on a candidate path."""

    kind: Literal["get", "put", "transfer", "free", "access", "unknown"] = "unknown"
    function: str = ""
    location: str = ""
    ref_delta: int = 0
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class RefcountPath(BaseModel):
    """Stable, structured UAF/refcount candidate path."""

    id: str
    summary: str
    events: list[ReferenceEvent] = Field(default_factory=list)
    net_delta: int = 0
    terminal_state: Literal["live", "released", "leaked", "uaf", "unknown"] = "unknown"
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


class PathCoverage(BaseModel):
    """Explicit analysis boundary and known unresolved call edges."""

    normal_paths_considered: bool = False
    error_paths_considered: bool = False
    transfer_paths_considered: bool = False
    async_paths_considered: bool = False
    concurrency_paths_considered: bool = False
    unresolved_indirect_calls: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class UafAnalysisContract(BaseModel):
    """P1 source of truth; legacy string paths remain compatibility output."""

    case_id: str = ""
    paths: list[RefcountPath] = Field(default_factory=list)
    coverage: PathCoverage = Field(default_factory=PathCoverage)
    excluded_paths: list[ExcludedPath] = Field(default_factory=list)
    max_likely_path_id: str = ""
    selection_rationale: str = ""
    reproduction_target_path_id: str = ""
    target_contexts: list[str] = Field(default_factory=list)
    legacy_unstructured: bool = False


class KernelExpertOutput(BaseModel):
    """Structured Kernel Expert output.

    This contract is JSON-first; incomplete output is blocked.
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
    execution_steps: list[ExecutionStep] = Field(default_factory=list)
    expected_signal: str = ""
    binaries_dir: str = ""
    build_status: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    # UAF/refcount investigations must preserve the complete path set even
    # when QEMU reproduction fails.  These fields are intentionally additive
    # and default to empty for non-UAF cases and legacy contracts.
    all_possible_paths: list[str] = Field(default_factory=list)
    max_likely_path: str = ""
    max_likely_path_rationale: str = ""
    excluded_paths: list[ExcludedPath] = Field(default_factory=list)
    path_analysis_required: bool = False
    path_analysis_scope: PathAnalysisScope = Field(default_factory=PathAnalysisScope)
    reproduction_target_path: str = ""
    uaf_analysis: UafAnalysisContract | None = None
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
