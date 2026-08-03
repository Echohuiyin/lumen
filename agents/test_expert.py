"""Test Expert node for isolated diagnostic userspace-C validation.

The node owns the Kernel Expert ↔ Test Expert feedback boundary.  It never
modifies a reproducer or accepts a generic kernel error as a successful
maintenance reproduction.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import os
import re
import shutil
import subprocess

from agents.contracts import (
    DetectionSignals,
    CallChainOracle,
    ExecutionStep,
    KernelExpertOutput,
    QemuRecipe,
    TestPlan,
    TestResultContract,
    model_to_dict,
)
from agents.llm_display import _format_agent_footer_text, _format_agent_header_text, ensure_output_dir, get_expert_output_file, set_session_dir
from agents.persistent_qemu import persistent_qemu_paths, run_persistent_qemu_test_plan
from graph.rn_state import MaintenanceWorkflowState


def _model_validate(model, value: dict):
    if hasattr(model, "model_validate"):
        return model.model_validate(value)
    return model.parse_obj(value)


def _attempt_runtime_root(session_dir: str, tryout: int) -> Path:
    if not session_dir:
        raise ValueError("missing session_dir for isolated QEMU try-out")
    session_path = Path(session_dir).expanduser().resolve()
    session_name = session_path.name
    if not session_name:
        raise ValueError("session_dir must name an isolated workflow session")

    # Large sparse guest copies may not fit on the project filesystem when
    # benchmark assets are being imported concurrently. Deployments can opt
    # into a writable scratch filesystem (tmpfs/NVMe) without changing the
    # evidence contract or baking a host-specific path into the code.
    configured_root = os.environ.get("LUMEN_QEMU_RUNTIME_ROOT", "").strip()
    root = Path(configured_root).expanduser().resolve() if configured_root else session_path
    if configured_root:
        root = root / session_name
    return root / "tryouts" / f"tryout-{tryout:02d}" / "qemu-ssh"


_IMAGE_DIGEST_CACHE: dict[tuple[str, int, int], str] = {}


def _sha256_file(path: Path) -> str:
    """Hash an image once per size/mtime so ten try-outs stay bounded."""
    stat = path.stat()
    key = (str(path), int(stat.st_size), int(stat.st_mtime_ns))
    cached = _IMAGE_DIGEST_CACHE.get(key)
    if cached:
        return cached
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    _IMAGE_DIGEST_CACHE[key] = value
    return value


def _copy_base_image(*, arch: str, runtime_root: Path, source_image: str = "") -> dict[str, str]:
    """Copy the declared case image into an isolated try-out directory."""
    base = persistent_qemu_paths(arch)
    attempt = persistent_qemu_paths(arch, runtime_root=runtime_root)
    image_source = Path(source_image).expanduser().resolve() if source_image else base.image
    if not image_source.is_file():
        raise FileNotFoundError(f"base image is missing: {image_source}")
    # A case-provided rootfs may have been built with a different authorized
    # key than the deployment default.  Pair an explicit image with the key
    # next to that image; silently mixing keys makes a healthy guest look like
    # a QEMU/SSH boot failure and would repeat the same error ten times.
    if source_image:
        sibling_key = image_source.parent / base.ssh_key.name
        if not sibling_key.is_file():
            # Benchmark asset export may copy the deployment image without
            # its key sidecar.  Reuse the deployment key only after proving
            # the two raw images are byte-identical; never mix keys for a
            # merely similar or independently built rootfs.
            if not base.image.is_file() or _sha256_file(image_source) != _sha256_file(base.image):
                raise FileNotFoundError(
                    f"declared rootfs has no co-located SSH key and is not "
                    f"byte-identical to configured base image: {image_source}; "
                    f"expected {sibling_key}"
                )
            key_source = base.ssh_key
            key_resolution = "deployment-default-byte-identical-image"
        else:
            key_source = sibling_key
            key_resolution = "case-co-located"
    else:
        key_source = base.ssh_key
        if not key_source.is_file():
            raise FileNotFoundError(f"base SSH key is missing: {key_source}")
        key_resolution = "deployment-base"
    attempt.image.parent.mkdir(parents=True, exist_ok=True)
    # Preserve sparse holes in the base image.  ``shutil.copy2`` expands the
    # 2-GiB sparse guest image to its logical size, which exhausts the host
    # after only a few ten-try-out loops.  Every try-out still gets its own
    # writable copy; ``cp --sparse=always`` changes only the representation.
    try:
        subprocess.run(
            ["cp", "--sparse=always", "--preserve=mode,timestamps",
             str(image_source), str(attempt.image)],
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise OSError(f"failed to create sparse writable image copy: {exc}") from exc
    shutil.copy2(key_source, attempt.ssh_key)
    attempt.ssh_key.chmod(0o600)
    return {
        "base_image": str(image_source),
        "attempt_image": str(attempt.image),
        "runtime_root": str(runtime_root),
        "ssh_key_source": str(key_source),
        "attempt_ssh_key": str(attempt.ssh_key),
        "ssh_key_resolution": key_resolution,
    }


def _build_detection_signals(
    kernel_contract: dict,
    expected_signal: str,
) -> DetectionSignals:
    """Build serial detection signals for legacy callers and contracts.

    Current Test Expert contracts carry fault signatures in the call-chain
    oracle.  Older unit callers still pass a plain contract dictionary and
    expected signal; retain a conservative, non-panic fallback for that API.
    """
    raw = kernel_contract.get("detection_signals") if kernel_contract else None
    if isinstance(raw, dict):
        try:
            return DetectionSignals(**raw)
        except Exception:
            pass
    if expected_signal.strip():
        return DetectionSignals(serial_signals=[expected_signal])
    return DetectionSignals(serial_signals=[
        "BUG:",
        "KASAN",
        "WARNING:",
        "hung_task",
        "blocked for more than",
    ])


def _is_inline_source_annotation(frame: str) -> bool:
    """Return whether a source-level frame is explicitly inlined.

    Inline helpers can explain a caller's source path without appearing as an
    independent runtime stack frame.  Only explicit annotations are filtered;
    ordinary function names containing ``inline`` remain required.
    """
    lowered = str(frame).lower()
    return bool(re.search(
        r"\[(?:[^]]*\bstatic\s+inline\b[^]]*|[^]]*\binlined\s+into\b[^]]*)\]"
        r"|\binlined\s+into\b|\binline\s+at\b",
        lowered,
    ))


def _strict_call_chain_oracle(contract: KernelExpertOutput) -> CallChainOracle:
    """Make the original log chain authoritative for a real Test Expert plan."""
    original = [
        str(frame).strip()
        for frame in contract.original_call_chain
        if str(frame).strip() and not _is_inline_source_annotation(frame)
    ]
    if not original:
        return contract.call_chain_oracle

    data = model_to_dict(contract.call_chain_oracle)
    allowed = {
        str(frame).strip()
        for frame in data.get("allowed_wrapper_frames") or []
        if str(frame).strip()
    }
    exact: list[str] = []
    for frame in original:
        if frame not in allowed and frame not in exact:
            exact.append(frame)
    exact_set = set(exact)

    configured_required = [
        str(frame).strip()
        for frame in data.get("required_frames") or []
        if (
            str(frame).strip()
            and not _is_inline_source_annotation(frame)
            and str(frame).strip() not in exact_set
        )
    ]
    required = [*exact, *configured_required]
    alternatives: list[list[str]] = []
    for group in data.get("required_frame_alternatives") or []:
        members = [
            str(frame).strip()
            for frame in group
            if str(frame).strip() and not _is_inline_source_annotation(frame)
        ]
        if members and not any(member in exact_set for member in members):
            alternatives.append(members)
    data["required_frames"] = required
    data["required_frame_alternatives"] = alternatives

    valid = set(required)
    valid.update(member for group in alternatives for member in group)
    order = [
        [str(pair[0]).strip(), str(pair[1]).strip()]
        for pair in data.get("required_frame_order") or []
        if len(pair) == 2 and str(pair[0]).strip() in valid and str(pair[1]).strip() in valid
    ]
    for pair in zip(exact, exact[1:]):
        pair_list = list(pair)
        if pair_list not in order:
            order.append(pair_list)
    data["required_frame_order"] = order
    return _model_validate(CallChainOracle, data)
_UNRESOLVED_REPRODUCER_ARG_RE = re.compile(r"<[^>\r\n]+>")


def _unresolved_reproducer_args(reproducer: UserspaceReproducer) -> list[str]:
    return [
        str(arg)
        for arg in reproducer.run_args
        if _UNRESOLVED_REPRODUCER_ARG_RE.search(str(arg))
    ]


def _build_plan(contract: KernelExpertOutput) -> TestPlan:
    reproducer = contract.reproducer
    unresolved_args = _unresolved_reproducer_args(reproducer)
    if unresolved_args:
        raise ValueError(
            "unresolved reproducer argument placeholder(s): "
            + ", ".join(sorted(set(unresolved_args)))
        )
    oracle = _strict_call_chain_oracle(contract)
    steps = [*contract.pressure_requirements, *contract.fault_injection_requirements]
    steps.append(ExecutionStep(
        type="run_binary",
        path=f"bin/{reproducer.output_binary}",
        args=list(reproducer.run_args),
    ))
    return TestPlan(
        target_arch=contract.target_arch,
        boot_kernel_path=contract.boot_kernel_path,
        rootfs_mode="ext4",
        rootfs_path=contract.rootfs_path,
        reproducer_dir=reproducer.source_dir,
        reproducer=reproducer,
        execution_steps=steps,
        expected_signal=contract.expected_signal,
        detection_signals=DetectionSignals(serial_signals=list(oracle.fault_signatures)),
        qemu_recipe=contract.qemu_recipe,
        reproduction_case_id=contract.uaf_analysis.case_id if contract.uaf_analysis else "maintenance-case",
        target_path_id=contract.uaf_analysis.reproduction_target_path_id if contract.uaf_analysis else f"tryout-{contract.tryout}",
        original_call_chain=list(contract.original_call_chain),
        target_contexts=[*oracle.target_subsystems, *oracle.target_objects],
        require_causal_reproduction=True,
        call_chain_oracle=oracle,
        root_cause=contract.root_cause,
    )


def _blocked_attempt(*, code: str, summary: str, tryout: int, artifacts: dict[str, str] | None = None) -> TestResultContract:
    return TestResultContract(
        status="blocked", code=code, attempts=tryout, summary=summary,
        artifacts=artifacts or {}, kernel_feedback=summary,
    )


def _promote_guest_capability_block(result: TestResultContract) -> TestResultContract:
    """Turn an explicit guest ABI/configuration failure into a terminal block.

    A missing target subsystem is not a call-chain mismatch: retrying the same
    image ten times cannot make ``mount(2)`` provide a disabled filesystem.
    Keep the raw SSH artifact and stop the Kernel/Test loop with an auditable
    reason instead of spending the retry budget on identical failures.
    """
    if result.status != "failed" or result.code not in {
        "FAILED_SIGNAL_NOT_FOUND",
        "FAILED_CALL_CHAIN_MISMATCH",
    }:
        return result

    evidence: list[tuple[str, str, str]] = []
    for key in ("ssh_output", "serial_log"):
        raw_path = str(result.artifacts.get(key, "") or "")
        if not raw_path:
            continue
        try:
            text = Path(raw_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        evidence.append((key, raw_path, text))

    # The runner emits this marker before compilation when a declared guest
    # component is unavailable.  Treat it as a terminal environment block:
    # retrying an unchanged rootfs cannot make the compiler appear.
    for key, raw_path, text in evidence:
        match = re.search(
            r"LUMEN_GUEST_COMPONENT_MISSING:([A-Za-z0-9_.+-]+):([A-Za-z0-9_.+-]+)",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        component_kind, component_name = match.groups()
        result.status = "blocked"
        result.code = "BLOCKED_GUEST_COMPONENT_MISSING"
        result.summary = (
            f"Guest is missing the declared {component_kind} component "
            f"{component_name}; install it in the selected QEMU image or "
            "select a compatible image before retrying."
        )
        result.kernel_feedback = result.summary
        result.artifacts.setdefault("capability_evidence", f"{key}:{raw_path}")
    # A userspace USB diagnostic can run successfully yet report that the
    # required target device was never enumerated. This is a QEMU hardware/
    # emulation capability block, not a call-chain mismatch; retrying the same
    # machine cannot create the missing Technisat interface.
    for key, raw_path, text in evidence:
        lowered = text.lower()
        target_device_missing = (
            "target usb device not observed" in lowered
            or "target usb device not found" in lowered
            or ("/dev/dvb/" in lowered and "no such file" in lowered)
            or ("/dev/bus/usb/" in lowered and "no such file" in lowered)
        )
        if target_device_missing:
            result.status = "blocked"
            result.code = "BLOCKED_GUEST_USB_DEVICE_MISSING"
            result.summary = (
                "The guest did not enumerate the required target USB device; "
                "provide a matching QEMU USB emulation or pass-through device "
                "before retrying this maintenance case."
            )
            result.kernel_feedback = result.summary
            result.artifacts.setdefault("capability_evidence", f"{key}:{raw_path}")
            return result

    # ``mount gadgetfs: No such device`` is the canonical signature when the
    # target kernel was built without CONFIG_USB_GADGETFS (or its UDC backend).
    # Restrict promotion to an explicit gadgetfs/USB ABI failure so an
    # unrelated boot-time message cannot suppress legitimate try-outs.
    # Deep suspend needs a PSCI system-suspend implementation.  A QEMU
    # virt guest can boot correctly yet expose only s2idle; in that case
    # the userspace ABI returns EINVAL before ct_kernel_exit is reachable.
    # This is a platform capability block, not a reproducer mismatch.
    for key, raw_path, text in evidence:
        lowered = text.lower()
        deep_suspend_unavailable = (
            "mem_sleep" in lowered
            and ("invalid argument" in lowered or "could not select deep" in lowered)
        ) or (
            "/sys/power/state" in lowered
            and "invalid argument" in lowered
        )
        if deep_suspend_unavailable:
            result.status = "blocked"
            result.code = "BLOCKED_GUEST_PLATFORM_UNSUPPORTED"
            result.summary = (
                "Guest exposes only s2idle/does not implement the PSCI deep "
                "suspend ABI; ct_kernel_exit cannot be reached by this QEMU platform."
            )
            result.kernel_feedback = result.summary
            result.artifacts.setdefault("capability_evidence", f"{key}:{raw_path}")
            return result

    # KVM/VGIC maintenance cases need a nested KVM device inside the guest.
    # QEMU's arm64 virt machine may boot normally while exposing no
    # /dev/kvm; retrying the same image cannot exercise the KVM userspace ABI.
    # Match only an explicit open(/dev/kvm) capability error so unrelated KVM
    # boot messages do not suppress a legitimate userspace try-out.
    for key, raw_path, text in evidence:
        lowered = text.lower()
        kvm_unavailable = re.search(
            r"open\(/dev/kvm\).*?(?:no such file|not found|operation not permitted|permission denied)",
            lowered,
            flags=re.DOTALL,
        )
        if kvm_unavailable:
            result.status = "blocked"
            result.code = "BLOCKED_GUEST_KVM_UNAVAILABLE"
            result.summary = (
                "Guest does not expose a usable /dev/kvm device; the selected "
                "QEMU platform cannot exercise the nested KVM/VGIC userspace ABI."
            )
            result.kernel_feedback = result.summary
            result.artifacts.setdefault("capability_evidence", f"{key}:{raw_path}")
            return result

    # SVE-dependent diagnostics can compile and run but explicitly report
    # that the guest CPU does not expose the arm64 SVE ABI.  This is a QEMU
    # CPU-model capability boundary, not a missing fault signature; retrying
    # an unchanged virt guest cannot enable SVE.
    for key, raw_path, text in evidence:
        lowered = text.lower()
        sve_unavailable = re.search(
            r"\b(?:arm64\s+)?sve\s+is\s+not\s+(?:exposed|available|supported)\b",
            lowered,
        ) or (
            re.search(r"\bsve\s*=\s*0\b", lowered)
            and re.search(r"\b(?:asimd|arm64)\b", lowered)
        )
        if sve_unavailable:
            result.status = "blocked"
            result.code = "BLOCKED_GUEST_SVE_UNAVAILABLE"
            result.summary = (
                "Guest does not expose the required arm64 SVE userspace ABI; "
                "select a QEMU CPU model with SVE before retrying this case."
            )
            result.kernel_feedback = result.summary
            result.artifacts.setdefault("capability_evidence", f"{key}:{raw_path}")
            return result

    for key, raw_path, text in evidence:
        lowered = text.lower()
        if "gadgetfs" in lowered and "no such device" in lowered:
            result.status = "blocked"
            result.code = "BLOCKED_GUEST_CAPABILITY_MISSING"
            result.summary = (
                "Guest rejected the gadgetfs userspace ABI (No such device); "
                "the booted kernel lacks the required gadgetfs/UDC capability."
            )
            result.kernel_feedback = result.summary
            result.artifacts.setdefault("capability_evidence", f"{key}:{raw_path}")
            return result
    return result


def _semantic_review(contract: KernelExpertOutput, result: TestResultContract) -> tuple[bool, str]:
    """Conservative Test Expert review; deterministic evidence remains primary."""
    if not result.call_chain_consistent:
        return False, "deterministic call-chain oracle did not match"
    if not contract.root_cause.strip():
        return False, "missing root-cause explanation prevents principle review"
    if not contract.call_chain_oracle.required_frames:
        return False, "missing required call-chain frames prevents principle review"
    return True, "observed post-start call chain satisfies the Kernel Expert oracle and root-cause context"


def _format_attempt(result: TestResultContract) -> str:
    lines = [
        f"TEST STATUS: {result.status}",
        f"CODE: {result.code}",
        f"TRY-OUT: {result.attempts}/10",
        f"CALL CHAIN CONSISTENT: {result.call_chain_consistent}",
        f"SEMANTIC REVIEW: {result.principle_consistent}",
        f"SUMMARY: {result.summary}",
    ]
    if result.missing_frames:
        lines.append("MISSING FRAMES: " + ", ".join(result.missing_frames))
    if result.kernel_feedback:
        lines.append("KERNEL FEEDBACK: " + result.kernel_feedback)
    for key, value in result.artifacts.items():
        lines.append(f"ARTIFACT {key}: {value}")
    return "\n".join(lines)


def test_expert_node(state: MaintenanceWorkflowState) -> dict:
    """Validate one userspace-C contract in a fresh QEMU image copy."""
    set_session_dir(state.get("session_dir"))
    ensure_output_dir()
    output_file = get_expert_output_file("test_expert")
    tryout = int(state.get("tryout_count", 0) or 0) + 1
    maximum = int(state.get("max_tryouts", 10) or 10)
    if maximum != 10:
        raise ValueError("max_tryouts is fixed at 10 by the maintenance workflow contract")

    try:
        contract = _model_validate(KernelExpertOutput, state.get("kernel_contract") or {})
    except Exception as exc:
        result = _blocked_attempt(code="BLOCKED_INVALID_KERNEL_CONTRACT", summary=str(exc), tryout=tryout)
    else:
        if contract.status != "ok":
            result = _blocked_attempt(
                code="BLOCKED_INVALID_KERNEL_CONTRACT",
                summary="Kernel Expert contract is not ready for Test Expert.",
                tryout=tryout,
            )
        elif contract.reproducer.artifact_type != "userspace" or contract.reproducer.language != "c" or contract.reproducer_module_path:
            result = _blocked_attempt(
                code="BLOCKED_NON_USERSPACE_REPRODUCER",
                summary="Test Expert accepts only an ok userspace C contract without kernel-module artifacts.",
                tryout=tryout,
            )
        else:
            try:
                plan = _build_plan(contract)
            except ValueError as exc:
                message = str(exc)
                code = (
                    "BLOCKED_UNRESOLVED_REPRODUCER_ARGUMENT"
                    if "unresolved reproducer argument" in message
                    else "BLOCKED_INVALID_EXECUTION_PLAN"
                )
                result = _blocked_attempt(code=code, summary=message, tryout=tryout)
            else:
                try:
                    runtime_root = _attempt_runtime_root(state.get("session_dir", ""), tryout)
                    image_artifacts = _copy_base_image(
                        arch=contract.target_arch,
                        runtime_root=runtime_root,
                        source_image=contract.rootfs_path,
                    )
                except (OSError, ValueError) as exc:
                    result = _blocked_attempt(code="BLOCKED_BASE_IMAGE_MISSING", summary=str(exc), tryout=tryout)
                else:
                    result = run_persistent_qemu_test_plan(
                        plan, attempt=tryout, runtime_root=runtime_root,
                    )
                    result.artifacts.update(image_artifacts)
                    result = _promote_guest_capability_block(result)
                    result.call_chain_consistent = bool(result.test_passed)
                    result.principle_consistent, result.semantic_review_reason = _semantic_review(contract, result)
                    result.test_passed = bool(result.call_chain_consistent and result.principle_consistent)
                    if result.call_chain_consistent and not result.principle_consistent:
                        result.status = "failed"
                        result.code = "FAILED_SEMANTIC_CALL_CHAIN_REVIEW"
                        result.summary = result.semantic_review_reason
                    if not result.test_passed and result.status not in {"blocked", "skipped"}:
                        if tryout >= maximum:
                            result.code = "FAILED_CALL_CHAIN_MISMATCH_AFTER_10_TRYOUTS"
                        elif not result.kernel_feedback:
                            result.kernel_feedback = "Review missing/reordered frames and revise the userspace trigger or declared injection plan."

    text = _format_attempt(result)
    with open(output_file, "w", encoding="utf-8") as handle:
        handle.write(_format_agent_header_text("测试专家", f"调用链验证 - 第{tryout}次"))
        handle.write(text + "\n")
        handle.write(_format_agent_footer_text("测试专家"))
    result_data = model_to_dict(result)
    previous_rounds = list(state.get("test_rounds", []) or [])
    previous_rounds.append(result_data)
    return {
        "test_result": text,
        "test_passed": result.test_passed,
        "test_attempts": tryout,
        "tryout_count": tryout,
        "test_rounds": previous_rounds,
        "test_contract": result_data,
        "test_attempt_contract": result_data,
        "call_chain_consistent": result.call_chain_consistent and result.principle_consistent,
        "test_feedback": result.kernel_feedback,
        "final_response": text if result.status in {"blocked", "skipped"} or tryout >= maximum else "",
    }
