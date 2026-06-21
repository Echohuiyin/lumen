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
    reproducer_dir: str = ""
    reproducer_module_path: str = ""
    test_script_path: str = ""
    expected_signal: str = ""


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
    reproducer_dir: str = ""
    reproducer_module_path: str = ""
    test_script_path: str = ""
    expected_signal: str = ""
    build_status: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blocked_reason: str = ""


class ToolExpertOutput(BaseModel):
    """Structured evidence wrapper for Crash/Lock/Log/Knowledge experts."""

    expert_type: str
    expert_name: str = ""
    status: WorkflowStatus = "degraded"
    summary: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
