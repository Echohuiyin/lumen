"""Test Expert node for isolated diagnostic userspace-C validation.

The node owns the Kernel Expert ↔ Test Expert feedback boundary.  It never
modifies a reproducer or accepts a generic kernel error as a successful
maintenance reproduction.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
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


def _host_qemu_capability_error(arch: str) -> str:
    """Return a terminal host capability error before any loop round starts."""
    normalized = {"amd64": "x86_64", "aarch64": "arm64"}.get(
        str(arch or "").strip().lower(), str(arch or "").strip().lower()
    )
    qemu_binary = {
        "x86_64": "qemu-system-x86_64",
        "arm64": "qemu-system-aarch64",
        "arm32": "qemu-system-arm",
    }.get(normalized, "")
    required = ["qemu-img"] + ([qemu_binary] if qemu_binary else [])
    missing = [name for name in required if shutil.which(name) is None]
    if not missing:
        return ""
    return (
        "Host QEMU capability is unavailable before the try-out: missing "
        + ", ".join(missing)
        + ". Install the declared QEMU tooling and rerun; this is an environment "
        "block and must not consume Kernel/Test loop rounds."
    )


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


def _copy_base_image(
    *, arch: str, runtime_root: Path, source_image: str = "", rootfs_mode: str = "declared",
) -> dict[str, str]:
    """Copy the selected rootfs into an isolated QEMU overlay.

    ``declared`` preserves per-case asset behavior for explicit compatibility
    tests.  Production uses ``deployment`` so every fixed case boots the
    SSH-capable ``debian.img`` selected by ``LUMEN_QEMU_IMAGE_ROOT``; the
    original case rootfs remains evidence only.
    """
    rootfs_mode = str(rootfs_mode or "declared").strip().lower()
    if rootfs_mode not in {"declared", "deployment"}:
        raise ValueError("rootfs_mode must be 'declared' or 'deployment'")
    use_declared = rootfs_mode == "declared"
    base = persistent_qemu_paths(arch)
    attempt = persistent_qemu_paths(arch, runtime_root=runtime_root)
    image_source = (
        Path(source_image).expanduser().resolve()
        if use_declared and source_image else base.image
    )
    if not image_source.is_file():
        raise FileNotFoundError(f"base image is missing: {image_source}")
    # A case-provided rootfs may have been built with a different authorized
    # key than the deployment default. Pair an explicit image with its key;
    # silently mixing keys makes a healthy guest look like an SSH failure.
    if use_declared and source_image:
        sibling_key = image_source.parent / base.ssh_key.name
        if not sibling_key.is_file():
            # Reuse the deployment key only after proving that the two raw
            # images are byte-identical; never mix keys for a similar image.
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
    # Keep the declared rootfs read-only and give each try-out a private
    # qcow2 overlay. The overlay remains with the round's evidence.
    try:
        info = subprocess.run(
            ["qemu-img", "info", "--output=json", str(image_source)],
            check=True, capture_output=True, text=True,
        )
        image_format = str(json.loads(info.stdout).get("format") or "").strip()
        if image_format not in {"raw", "qcow2"}:
            raise ValueError(f"unsupported declared rootfs format: {image_format or 'unknown'}")
        overlay = attempt.image.with_suffix(".qcow2")
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", "-F", image_format,
             "-b", str(image_source), str(overlay)],
            check=True, capture_output=True, text=True,
        )
        backing_hint = overlay.with_suffix(".backing")
        try:
            backing_hint.symlink_to(image_source)
        except FileExistsError:
            pass
    except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise OSError(f"failed to create qemu overlay for declared rootfs: {exc}") from exc
    shutil.copy2(key_source, attempt.ssh_key)
    attempt.ssh_key.chmod(0o600)
    return {
        "base_image": str(image_source),
        "attempt_image": str(overlay),
        "attempt_image_format": "qcow2",
        "runtime_root": str(runtime_root),
        "ssh_key_source": str(key_source),
        "attempt_ssh_key": str(attempt.ssh_key),
        "ssh_key_resolution": key_resolution,
    }


def _configured_rootfs_mode(config: dict | None) -> str:
    """Resolve the rootfs source mode without embedding a host path."""
    workflow = (config or {}).get("workflow") or {}
    raw = str(workflow.get("qemu_rootfs_mode", "deployment") or "deployment").strip()
    if raw.startswith("${"):
        raw = os.environ.get("LUMEN_QEMU_ROOTFS_MODE", "deployment")
    mode = raw.lower()
    if mode not in {"declared", "deployment"}:
        raise ValueError("workflow.qemu_rootfs_mode must be 'declared' or 'deployment'")
    return mode


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
        r"|\[\s*inline(?:\s*,|\s*\])"
        r"|\binlined\s+into\b|\binline\s+at\b",
        lowered,
    ))


def _frame_symbol(frame: str) -> str:
    """Return the symbol identity without offsets or source annotations."""
    value = str(frame).strip().lstrip("?* ")
    value = re.sub(r"^\s*(?:pc|lr|rip)\s*:\s*", "", value, flags=re.IGNORECASE)
    match = re.match(r"([A-Za-z_][A-Za-z0-9_.]*)", value)
    return match.group(1) if match else value


def _is_arch_wrapper_frame(frame: str) -> bool:
    """Recognize generic syscall/architecture entry wrappers.

    These wrappers are useful context but are not the maintenance subsystem
    path.  The classification is based on stable symbol naming conventions,
    not on a host path or a case-specific function name.
    """
    symbol = _frame_symbol(frame)
    return bool(re.match(
        r"^(?:__?(?:do|se|arm64|x64|ia32|x86_64)_sys_|"
        r"__invoke_syscall$|invoke_syscall$|"
        r"el[0-9](?:t)?(?:_|$)|do_el[0-9](?:_|$)|"
        r"entry_SYSCALL|ret_to_user|syscall_(?:enter|exit))",
        symbol,
    ))


def _strict_call_chain_oracle(contract: KernelExpertOutput) -> CallChainOracle:
    """Make the original log chain authoritative for a real Test Expert plan."""
    raw_original = [
        str(frame).strip()
        for frame in contract.original_call_chain
        if str(frame).strip()
    ]
    if not raw_original:
        return contract.call_chain_oracle

    data = model_to_dict(contract.call_chain_oracle)
    allowed = {
        str(frame).strip()
        for frame in data.get("allowed_wrapper_frames") or []
        if str(frame).strip()
    }
    allowed_symbols = {_frame_symbol(frame) for frame in allowed}
    for frame in raw_original:
        if _is_arch_wrapper_frame(frame):
            allowed.add(frame)
            allowed_symbols.add(_frame_symbol(frame))

    original = [
        frame for frame in raw_original
        if not _is_inline_source_annotation(frame)
        and _frame_symbol(frame) not in allowed_symbols
    ]
    requested_top = [
        str(frame).strip()
        for frame in data.get("required_top_frames") or []
        if str(frame).strip()
        and not _is_inline_source_annotation(frame)
        and _frame_symbol(frame) not in allowed_symbols
    ]
    requested_symbols = {_frame_symbol(frame) for frame in requested_top}
    if requested_symbols:
        by_symbol = {_frame_symbol(frame): frame for frame in original}
        # Preserve the order declared by the Kernel Expert, not set order.
        exact = [
            by_symbol[_frame_symbol(frame)] for frame in requested_top
            if _frame_symbol(frame) in by_symbol
        ]
    else:
        # Legacy contracts have only the complete chain. Require the first
        # three concrete fault-site/caller frames and allow lower context to vary.
        exact = original[:3]
    exact_symbols = {_frame_symbol(frame) for frame in exact}

    # Only the declared/derived top frames are mandatory. Lower frames in
    # required_frames are retained as audit input but are context-sensitive.
    required = list(exact)
    alternatives: list[list[str]] = []
    for group in data.get("required_frame_alternatives") or []:
        members: list[str] = []
        member_symbols: set[str] = set()
        for raw_frame in group:
            frame = str(raw_frame).strip()
            symbol = _frame_symbol(frame)
            if (
                not frame
                or _is_inline_source_annotation(frame)
                or symbol in allowed_symbols
                or symbol in exact_symbols
                or symbol in member_symbols
            ):
                continue
            members.append(frame)
            member_symbols.add(symbol)
        if members:
            members = [
                member for member in members
                if _frame_symbol(member) in exact_symbols
            ]
        if members:
            alternatives.append(members)
    data["required_frames"] = required
    data["required_top_frames"] = required
    data["required_frame_alternatives"] = alternatives
    data["allowed_wrapper_frames"] = sorted(allowed)

    valid_symbols = {_frame_symbol(frame) for frame in required}
    valid_symbols.update(
        _frame_symbol(member) for group in alternatives for member in group
    )
    order = [
        [str(pair[0]).strip(), str(pair[1]).strip()]
        for pair in data.get("required_frame_order") or []
        if (
            len(pair) == 2
            and _frame_symbol(pair[0]) in valid_symbols
            and _frame_symbol(pair[1]) in valid_symbols
        )
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
        binaries_dir=contract.binaries_dir,
        reproducer=reproducer,
        execution_steps=steps,
        expected_signal=contract.expected_signal,
        test_assets_dir=contract.test_assets_dir,
        detection_signals=DetectionSignals(serial_signals=list(oracle.fault_signatures)),
        qemu_recipe=contract.qemu_recipe,
        reproduction_case_id=contract.uaf_analysis.case_id if contract.uaf_analysis else "maintenance-case",
        target_path_id=contract.uaf_analysis.reproduction_target_path_id if contract.uaf_analysis else f"tryout-{contract.tryout}",
        original_call_chain=list(contract.original_call_chain),
        target_contexts=[*oracle.target_subsystems, *oracle.target_objects],
        require_causal_reproduction=True,
        call_chain_oracle=oracle,
        verified_setup=list(contract.verified_setup),
        best_call_chain_prefix=list(contract.best_call_chain_prefix),
        root_cause=contract.root_cause,
    )


def _blocked_attempt(*, code: str, summary: str, tryout: int, artifacts: dict[str, str] | None = None) -> TestResultContract:
    return TestResultContract(
        status="blocked", code=code, attempts=tryout, summary=summary,
        artifacts=artifacts or {}, kernel_feedback=summary,
    )


def _promote_guest_capability_block(result: TestResultContract) -> TestResultContract:
    """Classify explicit guest ABI/configuration evidence before retrying.

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

    # A userspace diagnostic may discover an optional filesystem utility only
    # after it has started (for example ``mkfs.bcachefs``).  Keep this explicit
    # marker in the same terminal capability class; retrying an unchanged
    # image cannot make an absent executable appear.  Do not broaden this to
    # arbitrary stderr text, because normal reproducer failures remain valid
    # try-outs and must still reach the Kernel Expert loop.
    for key, raw_path, text in evidence:
        match = re.search(
            r"LUMEN_BLOCKED:\s*no\s+executable\s+([A-Za-z0-9_.+-]+)",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        component_name = match.group(1)
        result.status = "blocked"
        result.code = "BLOCKED_GUEST_COMPONENT_MISSING"
        result.summary = (
            f"Guest is missing the userspace executable {component_name}; "
            "install it in the selected QEMU image or select a compatible "
            "image before retrying."
        )
        result.kernel_feedback = result.summary
        result.artifacts.setdefault("capability_evidence", f"{key}:{raw_path}")
        return result

    # Fault injection is part of the causal test plan.  A missing or
    # read-only debugfs control plane must not be mistaken for a userspace
    # trigger mismatch and retried against the same guest image.
    for key, raw_path, text in evidence:
        match = re.search(
            r"LUMEN_GUEST_FAULT_INJECTION_UNAVAILABLE:"
            r"([A-Za-z0-9_.+-]+):([A-Za-z0-9_.+-]+)",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        profile, target = match.groups()
        result.status = "blocked"
        result.code = "BLOCKED_GUEST_FAULT_INJECTION_UNAVAILABLE"
        result.summary = (
            f"Guest cannot configure the required {profile} fault-injection "
            f"control plane (target {target}); enable the matching kernel "
            "debugfs controls or select a compatible QEMU image before retrying."
        )
        result.kernel_feedback = result.summary
        result.artifacts.setdefault("capability_evidence", f"{key}:{raw_path}")
        return result
    # A successfully compiled pthread probe that cannot create/join a worker
    # is deterministic guest ABI evidence. Keep this retryable so Kernel
    # Expert can switch to process workers or a single-process trigger; do not
    # relabel the probe's SIGSEGV as an unsafe diagnostic C program.
    for key, raw_path, text in evidence:
        match = re.search(
            r"LUMEN_GUEST_RUNTIME_INCOMPATIBLE:([A-Za-z0-9_.+-]+)",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        capability = match.group(1)
        result.status = "failed"
        result.code = "FAILED_GUEST_RUNTIME_INCOMPATIBLE"
        result.summary = (
            f"Guest runtime capability check failed for {capability}; "
            "this is environment evidence, not a reproducer C-safety verdict. "
            "Use a compatible rootfs/kernel or revise the trigger to avoid "
            "the unavailable userspace runtime ABI."
        )
        result.kernel_feedback = result.summary
        result.artifacts.setdefault("capability_evidence", f"{key}:{raw_path}")
        return result

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

    # OCFS2 move-extents diagnostics require both the mkfs.ocfs2 userspace
    # helper and a writable OCFS2 mount.  A plain ext4 QEMU rootfs cannot
    # provide that filesystem ABI; retrying the same image cannot change the
    # visible mount set, so stop before spending the remaining try-outs on an
    # identical capability failure.
    for key, raw_path, text in evidence:
        lowered = text.lower()
        ocfs2_tool_missing = (
            "mkfs.ocfs2" in lowered
            and (
                "no such file or directory" in lowered
                or "not found" in lowered
            )
        )
        ocfs2_mount_missing = (
            "no writable ocfs2 mount" in lowered
            or "mkfs.ocfs2 is not installed" in lowered
            or (
                "diagnostic prerequisites unavailable" in lowered
                and "ocfs2" in lowered
            )
        )
        if ocfs2_tool_missing:
            result.status = "blocked"
            result.code = "BLOCKED_GUEST_OCFS2_TOOL_MISSING"
            result.summary = (
                "Guest is missing the mkfs.ocfs2 helper from ocfs2-tools; "
                "rebuild the QEMU image with that package before retrying "
                "this OCFS2 move-extents case."
            )
            result.kernel_feedback = result.summary
            result.artifacts.setdefault("capability_evidence", f"{key}:{raw_path}")
            return result
        if ocfs2_mount_missing:
            result.status = "blocked"
            result.code = "BLOCKED_GUEST_OCFS2_MOUNT_MISSING"
            result.summary = (
                "Guest has no writable OCFS2 mount for the move-extents ABI; "
                "provide an OCFS2-formatted writable disk or mount fixture "
                "before retrying this maintenance case."
            )
            result.kernel_feedback = result.summary
            result.artifacts.setdefault("capability_evidence", f"{key}:{raw_path}")
            return result

    # ``mount gadgetfs: No such device`` is the canonical signature when the
    # target kernel was built without CONFIG_USB_GADGETFS (or its UDC backend).
    # Bluetooth SCO diagnostics can run normally while the guest has no HCI
    # controller. An explicit ENODEV from the HCI ioctl is a QEMU hardware
    # capability boundary, not a missing lockdep signature; retrying the same
    # guest cannot create a controller.
    for key, raw_path, text in evidence:
        lowered = text.lower()
        hci_device_missing = (
            re.search(r"\bfirst_hci_errno\s*=\s*(?:19|enodev)\b", lowered)
            and re.search(r"\bhci_(?:down|up)_ok\s*=\s*0\b", lowered)
        ) or re.search(
            r"\b(?:hci|bluetooth)[^\n]{0,120}"
            r"(?:no such device|no device|not found)\b",
            lowered,
        )
        if hci_device_missing:
            result.status = "blocked"
            result.code = "BLOCKED_GUEST_BLUETOOTH_HCI_MISSING"
            result.summary = (
                "Guest has no usable Bluetooth HCI controller; provide a "
                "QEMU Bluetooth controller or pass-through device before "
                "retrying this SCO maintenance case."
            )
            result.kernel_feedback = result.summary
            result.artifacts.setdefault("capability_evidence", f"{key}:{raw_path}")
            return result

    # A userspace SCO diagnostic that makes attempts but records no
    # successful connection cannot reach the SCO teardown path. Treat this as
    # an unavailable Bluetooth SCO/HCI setup, not as a retryable call-chain
    # mismatch; a different controller or peer is required.
    for key, raw_path, text in evidence:
        lowered = text.lower()
        sco_unavailable = (
            re.search(
                r"\bsco_connect_attempts\s*=\s*[1-9][0-9]*"
                r".*?\bsuccesses\s*=\s*0\b"
                r".*?\berrors\s*=\s*[1-9][0-9]*",
                lowered,
                flags=re.DOTALL,
            )
            or re.search(
                r"\bsco\b[^\n]*(?:no route to host|network is unreachable|"
                r"no such device|operation not supported|connection refused)",
                lowered,
            )
        )
        if sco_unavailable:
            result.status = "blocked"
            result.code = "BLOCKED_GUEST_BLUETOOTH_SCO_UNAVAILABLE"
            result.summary = (
                "Guest made SCO connection attempts but established none; "
                "the QEMU Bluetooth HCI/SCO setup cannot reach the target "
                "teardown path. Provide a usable controller and peer before "
                "retrying this maintenance case."
            )
            result.kernel_feedback = result.summary
            result.artifacts.setdefault("capability_evidence", f"{key}:{raw_path}")
            return result

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
        sve_unavailable = sve_unavailable or re.search(
            r"\blumen_repro_blocked\s*:\s*sve_prctl\b",
            lowered,
        ) or re.search(
            r"\bsve(?:[_\s-]+(?:abi|prctl))?\s*(?:=|:)\s*unavailable\b"
            r"|\bsve[_\s-]+unavailable\b",
            lowered,
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


def _historical_userspace_crash_feedback(previous_rounds: list[dict] | None) -> str:
    """Keep prior userspace-crash constraints visible across loop iterations."""
    excerpts: list[str] = []
    for round_data in previous_rounds or []:
        if not isinstance(round_data, dict):
            continue
        feedback = str(round_data.get("kernel_feedback") or "")
        marker = "INVALID_USERSPACE_CRASH"
        marker_at = feedback.find(marker)
        if marker_at < 0:
            continue
        excerpt = feedback[marker_at:marker_at + 640].strip()
        if excerpt and excerpt not in excerpts:
            excerpts.append(excerpt)
        if len(excerpts) >= 3:
            break
    if not excerpts:
        return ""
    return (
        "HISTORICAL_USERSPACE_CRASH_CONSTRAINT: a previous try-out crashed in the guest userspace. "
        "Do not reintroduce the same unsafe construct; audit and repair C safety before changing the kernel oracle.\n"
        + "\n".join(excerpts)
    )


def _mount_detach_path_feedback(result: TestResultContract) -> str:
    """Explain a lost detached-mount dentry when the C trigger uses AT_FDCWD.

    GadgetFS lifetime failures require an open lookup to retain the detached
    mount's dentry while the final ``dev_data`` reference is released.  An
    absolute ``openat(AT_FDCWD, ...)`` after ``MNT_DETACH`` resolves through
    the namespace again and can silently hit the underlying directory instead.
    This is feedback only: the deterministic oracle still decides pass/fail.
    """
    artifacts = result.artifacts or {}
    stage = str(artifacts.get("poc_stage", "") or "").strip()
    if not stage:
        return ""
    source_root = Path(stage) / "reproducer"
    try:
        sources = sorted(source_root.glob("*.c"))
    except OSError:
        return ""
    for source in sources:
        try:
            text = source.read_text(encoding="utf-8", errors="replace")[:128 * 1024]
        except OSError:
            continue
        if "MNT_DETACH" not in text or "gadgetfs" not in text.lower():
            continue
        if not re.search(r"\bopenat\s*\(\s*AT_FDCWD\b", text):
            continue
        return (
            "MOUNT_DENTRY_LIFETIME_FEEDBACK: the gadgetfs trigger uses "
            "openat(AT_FDCWD, ...) while relying on MNT_DETACH. After the "
            "detach, an absolute path can resolve to the underlying directory "
            "instead of retaining the mounted dentry, so it cannot exercise "
            "gadget_dev_open after dev_release frees dev_data. Preserve an "
            "O_PATH|O_DIRECTORY fd for the mounted root before detach and use "
            "openat(dirfd, endpoint) while workers remain active across the "
            "final holder close; keep the missing-frame result unchanged."
        )
    return ""


def _augment_kernel_feedback(
    result: TestResultContract, previous_rounds: list[dict] | None = None,
) -> str:
    """Add bounded post-start runtime evidence to the next Kernel turn."""
    feedback = str(result.kernel_feedback or result.summary or "").strip()
    if result.test_passed:
        return feedback
    historical = _historical_userspace_crash_feedback(previous_rounds)
    runtime_text: list[str] = []
    guest_runtime_incompatibility: list[str] = []
    for artifact_name in ("serial_log", "ssh_output"):
        runtime_path = str((result.artifacts or {}).get(artifact_name, "") or "").strip()
        if not runtime_path:
            continue
        try:
            text = Path(runtime_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(
            r"LUMEN_GUEST_RUNTIME_INCOMPATIBLE:([A-Za-z0-9_.+-]+)",
            text,
            flags=re.IGNORECASE,
        ):
            capability = match.group(1)
            if capability not in guest_runtime_incompatibility:
                guest_runtime_incompatibility.append(capability)
        marker_at = text.find("LUMEN_REPRO_START:")
        if artifact_name == "serial_log":
            # Boot failures happen before the runner marker is emitted.
            # Their serial output may contain unrelated init SIGSEGV lines.
            # Do not feed those lines into userspace-POC safety feedback.
            if marker_at < 0:
                continue
            text = text[marker_at:]
        runtime_text.append(text)
    if historical and not runtime_text:
        return f"{historical}\n{feedback}".strip()
    if not runtime_text:
        return f"{historical}\n{feedback}".strip() if historical else feedback
    mount_dentry_feedback = _mount_detach_path_feedback(result)
    if mount_dentry_feedback and mount_dentry_feedback not in feedback:
        feedback = f"{mount_dentry_feedback}\n{feedback}".strip()
    evidence_re = re.compile(
        r"(?:segfault|kasan|j1939|lumen_guest_component_missing|"
        r"lumen_guest_runtime_incompatible|"
        r"cannot|failed|error|warning|bug:|no such|abort|connection exists|"
        r"unknown parameter|invalid(?:\s+\S+){0,3}|resource busy|"
        r"lumen_diagnostic_[a-z0-9_]+)",
        re.IGNORECASE,
    )
    evidence: list[str] = []
    for line in "\n".join(runtime_text).splitlines():
        text = line.strip()
        if not text or not evidence_re.search(text):
            continue
        if text not in evidence:
            evidence.append(text[:320])
        if len(evidence) >= 8:
            break
    fixture_size_error = any(
        re.search(
            # ``mkfs.nilfs2`` prints ``Device Size:<bytes>`` on every
            # successful format. Treating that informational line as a
            # failure kept stale FIXTURE_SIZE_TOO_SMALL feedback alive after
            # a later try-out had already crossed the minimum size. Match
            # only an explicit rejection or a required/minimum-size
            # diagnostic instead of the generic phrase ``device size``.
            r"(?:too\s+small\s+(?:device|filesystem)|"
            r"(?:device|filesystem)\s+(?:is\s+)?too\s+small|"
            r"(?:minimum|required)\s+(?:device\s+)?size\s*(?:[:=]|\b)|"
            r"size\s+(?:must\s+be|needs?\s+to\s+be))",
            line,
            re.IGNORECASE,
        )
        for line in "\n".join(runtime_text).splitlines()
    )
    # Preserve the formatter's exact lower bound when it is available.  A
    # reproducer may print an earlier sector-derived estimate, but mkfs is the
    # authoritative component deciding whether the image can be created.  If
    # we drop its ``required size`` line, Kernel Expert can choose a size that
    # is only a few sectors too small and repeat the same pre-kernel failure.
    formatter_required_sizes: list[int] = []
    formatter_required_re = re.compile(
        r"\b(?:required|minimum)\s+(?:device\s+)?size\s*[:=]\s*"
        r"([0-9][0-9_,]*)\b",
        re.IGNORECASE,
    )
    for line in "\n".join(runtime_text).splitlines():
        for match in formatter_required_re.finditer(line):
            try:
                required = int(match.group(1).replace(",", ""))
            except ValueError:
                continue
            if 0 < required <= (1 << 50) and required not in formatter_required_sizes:
                formatter_required_sizes.append(required)
    if fixture_size_error:
        size_feedback = (
            "FIXTURE_SIZE_TOO_SMALL: the guest formatter rejected the userspace "
            "fixture size. Read the reported requirement, recreate the image above "
            "that size inside the bounded C fixture, and do not repeat an unchanged "
            "image or interpret the formatter error as a kernel path."
        )
        if formatter_required_sizes:
            required = max(formatter_required_sizes)
            size_feedback += (
                f" Explicit formatter requirement: required size={required} bytes. "
                "Treat this value as authoritative, then add the mandated safety "
                "margin before selecting IMAGE_BYTES."
            )
        if size_feedback not in feedback:
            feedback = f"{size_feedback}\n{feedback}".strip()

    # Once a guest has formatted a fixture successfully, later diagnostic
    # revisions must keep that proven lower bound.  Without this monotonic
    # handoff, a no-signal retry can silently shrink the image and reintroduce
    # a formatter failure before the kernel path is exercised.
    established_fixture_sizes: list[int] = []
    runtime_snapshot = "\n".join(runtime_text)
    # Not every valid userspace fixture emits the optional FORMAT_OK marker.
    # LOOP_READY or REPRO_START is emitted only after the fixture has passed
    # its formatter/loop setup in the current C contract.  Keep the explicit
    # marker as the strongest signal, but accept these structured milestones
    # so a later trigger revision cannot regress a proven image size merely
    # because the diagnostic used a different setup marker.
    fixture_setup_succeeded = bool(
        re.search(r"\bLUMEN_FORMAT_OK\b", runtime_snapshot, re.IGNORECASE)
        or re.search(r"\bLUMEN_LOOP_READY\b", runtime_snapshot, re.IGNORECASE)
        or re.search(r"\bLUMEN_MOUNT_OK\b", runtime_snapshot, re.IGNORECASE)
        or re.search(r"\bLUMEN_REPRO_START\b", runtime_snapshot, re.IGNORECASE)
    )
    fixture_size_markers = re.compile(
        r"(?:\bLUMEN_FIXTURE\b|\bLUMEN_SETUP\b|\bfixture_size_check\b|"
        r"\bformatter=[^\n]*\bmkfs\.nilfs2\b|"
        r"\bLUMEN_REPRO_START\b[^\n]*\bfixture\b)"
        r"[^\n]*\bimage_bytes=(\d+)",
        flags=re.IGNORECASE,
    )
    if fixture_setup_succeeded:
        for match in fixture_size_markers.finditer(runtime_snapshot):
            try:
                image_bytes = int(match.group(1))
            except ValueError:
                continue
            if 0 < image_bytes <= (1 << 50) and image_bytes not in established_fixture_sizes:
                established_fixture_sizes.append(image_bytes)
    if established_fixture_sizes:
        established = max(established_fixture_sizes)
        fixture_feedback = (
            f"FIXTURE_SIZE_ESTABLISHED: a prior guest run successfully formatted "
            f"a fixture of {established} bytes. Preserve IMAGE_BYTES at least this "
            "large in every later revision; do not shrink or replace the proven "
            "fixture while changing only the trigger/pressure plan."
        )
        if fixture_feedback not in feedback:
            feedback = f"{fixture_feedback}\n{feedback}".strip()
    if guest_runtime_incompatibility:
        capability_text = ", ".join(guest_runtime_incompatibility)
        environment_feedback = (
            "GUEST_RUNTIME_INCOMPATIBLE: environment evidence shows that the "
            f"guest cannot execute the required runtime capability ({capability_text}); "
            "do not classify its probe crash as userspace C unsafety."
        )
        if environment_feedback not in feedback:
            feedback = f"{environment_feedback}\n{feedback}".strip()
    if not evidence:
        return feedback
    summary = "Runtime evidence: " + " | ".join(evidence)
    if any(
        re.search(
            r"\b(?:segfault|sigsegv|stack\s+smashing|double\s+free|aborted\s*\(core\s+dumped\))\b",
            line,
            re.IGNORECASE,
        )
        for line in evidence
    ) and not guest_runtime_incompatibility:
        feedback = (
            "INVALID_USERSPACE_CRASH: the guest reproducer crashed in userspace; "
            "repair its C safety and lifetime handling before changing the kernel oracle.\n"
            + feedback
        )
    if historical and historical not in feedback:
        feedback = f"{historical}\n{feedback}".strip()
    if summary in feedback:
        return feedback
    return f"{feedback}\n{summary}".strip()


_SETUP_MARKER_TOKENS = (
    "ASSET", "BIND", "CONFIG", "CONNECT", "CREATE", "DEVICE", "FIXTURE",
    "INIT", "INTERFACE", "MOUNT", "OPEN", "READY", "SETUP", "SOCKET",
    "UP", "VCAN",
)
_TERMINAL_MARKERS = {"START", "DONE", "ERROR", "FAIL", "RESULT", "SUMMARY"}


def _read_reproducer_setup_markers(round_data: dict) -> set[str]:
    """Return stable setup markers emitted by one guest try-out.

    Kernel Expert retries are incremental revisions, not fresh independent
    experiments. LUMEN_REPRO_* setup markers are the only durable,
    userspace-owned evidence that a prerequisite (for example creating and
    bringing up vcan0) was actually established in the guest. Values such
    as fd numbers, ports, and iteration counters are intentionally discarded;
    the marker names are the stable contract.
    """
    artifacts = round_data.get("artifacts") or {}
    markers: set[str] = set()
    for key in ("ssh_output", "serial_log"):
        raw_path = str(artifacts.get(key, "") or "")
        if not raw_path:
            continue
        try:
            text = Path(raw_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        marker_pattern = re.compile(
            r"\b(?:LUMEN_REPRO_|LUMEN_)([A-Z][A-Z0-9_]*)\b"
        )
        generic_setup_markers = {
            "FIXTURE", "SETUP", "LOOP_READY", "MOUNT_OK", "FORMAT_OK",
            "ASSET_READY", "DEVICE_READY",
        }
        for match in marker_pattern.finditer(text):
            raw = match.group(0)
            name = match.group(1)
            if name in _TERMINAL_MARKERS:
                continue
            if raw.startswith("LUMEN_REPRO_"):
                if any(token in name for token in _SETUP_MARKER_TOKENS):
                    markers.add(name)
            elif (
                name in generic_setup_markers
                or (
                    any(token in name for token in _SETUP_MARKER_TOKENS)
                    and not any(
                        token in name
                        for token in ("FAIL", "ERROR", "START", "DONE", "RESULT", "SUMMARY")
                    )
                )
            ):
                markers.add(name)
    return markers


def _apply_reproducer_regression_guard(
    result: TestResultContract,
    previous_rounds: list[dict],
) -> TestResultContract:
    """Reject an incremental POC that drops a previously verified setup step.

    A later Kernel Expert revision must preserve the last round's verified
    guest prerequisites and change only the trigger under investigation.
    Missing setup markers are a deterministic regression, not evidence that
    the kernel path disappeared. The raw try-out remains archived and the
    feedback sends the model back with an actionable, minimal-change request.
    """
    prior_markers: set[str] = set()
    for previous in reversed(previous_rounds):
        prior_markers = _read_reproducer_setup_markers(previous)
        if prior_markers:
            break
    if not prior_markers or result.status != "failed":
        return result

    current_markers = _read_reproducer_setup_markers(model_to_dict(result))
    missing = sorted(prior_markers - current_markers)
    if not missing:
        return result

    result.status = "failed"
    result.code = "FAILED_REPRODUCER_REGRESSION"
    result.summary = (
        "The revised userspace reproducer dropped setup prerequisites already "
        "verified by an earlier QEMU try-out: " + ", ".join(missing)
    )
    result.kernel_feedback = (
        "REPRODUCER_REGRESSION_GUARD: preserve the previous userspace source "
        "and all verified setup steps, then make only an incremental trigger "
        "change. Missing setup markers: " + ", ".join(missing)
    )
    result.artifacts["regression_required_setup_markers"] = ",".join(sorted(prior_markers))
    result.artifacts["regression_missing_setup_markers"] = ",".join(missing)
    return result


def _canonical_progress_frame(value: str) -> str:
    value = str(value or "").strip().lstrip("?* ")
    return re.split(r"\s+", value, maxsplit=1)[0]


def _validate_incremental_kernel_contract(
    contract: KernelExpertOutput, contract_history: list[dict],
) -> str:
    """Require an explicit delta and setup inheritance on Kernel retries."""
    if len(contract_history) < 2:
        return ""
    try:
        previous = _model_validate(KernelExpertOutput, contract_history[-2])
    except Exception as exc:
        return f"invalid prior Kernel contract history: {exc}"
    if not str(contract.change_from_previous_tryout or "").strip():
        return (
            "Kernel Expert retry contract omitted change_from_previous_tryout; "
            "the new POC must state one evidence-backed incremental change."
        )
    prior_setup = {str(item).strip() for item in previous.verified_setup if str(item).strip()}
    current_setup = {str(item).strip() for item in contract.verified_setup if str(item).strip()}
    missing = sorted(prior_setup - current_setup)
    if missing:
        return (
            "Kernel Expert retry contract dropped verified_setup entries already "
            "declared by the previous contract: " + ", ".join(missing)
        )
    return ""


def _best_call_chain_prefix(
    contract: KernelExpertOutput, result: TestResultContract,
) -> list[str]:
    """Return the longest ordered required-frame prefix seen in this round."""
    oracle = contract.call_chain_oracle
    required = list(oracle.required_top_frames or oracle.required_frames)
    found = {_canonical_progress_frame(item) for item in result.required_frames_found}
    prefix: list[str] = []
    for frame in required:
        canonical = _canonical_progress_frame(frame)
        if canonical and canonical in found:
            prefix.append(frame)
        else:
            break
    return prefix


def _apply_progress_metadata(
    result: TestResultContract,
    contract: KernelExpertOutput,
    previous_rounds: list[dict],
) -> TestResultContract:
    """Attach setup/prefix progress and stop repeated or regressing branches."""
    result.verified_setup = sorted(_read_reproducer_setup_markers(model_to_dict(result)))
    result.best_call_chain_prefix = _best_call_chain_prefix(contract, result)
    result.plan.verified_setup = list(result.verified_setup)
    result.plan.best_call_chain_prefix = list(result.best_call_chain_prefix)
    if result.status in {"blocked", "skipped"}:
        result.progress_kind = "terminal"
        result.no_progress_streak = 0
        return result
    if not previous_rounds:
        result.progress_kind = "initial"
        result.no_progress_streak = 0
        return result

    prior_setup = {
        str(item).strip()
        for item in (previous_rounds[-1].get("verified_setup") or [])
        if str(item).strip()
    }
    prior_prefix_len = max(
        (len(item.get("best_call_chain_prefix") or []) for item in previous_rounds),
        default=0,
    )
    current_setup = set(result.verified_setup)
    current_prefix_len = len(result.best_call_chain_prefix)
    if (prior_setup and not prior_setup.issubset(current_setup)) or current_prefix_len < prior_prefix_len:
        result.progress_kind = "retracted"
        result.no_progress_streak = 0
        if result.code != "FAILED_REPRODUCER_REGRESSION":
            result.code = "FAILED_REPRODUCER_REGRESSION"
            result.summary = (
                "The current round regressed previously verified setup or the "
                "best call-chain prefix; preserve both before changing the trigger."
            )
        return result
    if current_prefix_len > prior_prefix_len:
        result.progress_kind = "call_chain_prefix_growth"
        result.no_progress_streak = 0
    elif current_setup - prior_setup:
        result.progress_kind = "setup_progress"
        result.no_progress_streak = 0
    elif result.signal_after_start or result.target_context_matched:
        result.progress_kind = "runtime_evidence"
        result.no_progress_streak = 0
    else:
        result.progress_kind = "no_progress"
        prior_streak = int(previous_rounds[-1].get("no_progress_streak", 0) or 0)
        result.no_progress_streak = prior_streak + 1
        if result.no_progress_streak >= 2:
            result.status = "blocked"
            result.code = "BLOCKED_PROGRESS_GATE"
            result.summary = (
                "Two consecutive try-outs produced no setup, call-chain-prefix, "
                "or target-context progress; stop this branch."
            )
    return result


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
    lines.append("VERIFIED SETUP: " + ", ".join(result.verified_setup))
    lines.append("BEST CALL CHAIN PREFIX: " + " -> ".join(result.best_call_chain_prefix))
    lines.append("PROGRESS: " + result.progress_kind + f" (no_progress_streak={result.no_progress_streak})")
    if result.kernel_feedback:
        lines.append("KERNEL FEEDBACK: " + result.kernel_feedback)
    for key, value in result.artifacts.items():
        lines.append(f"ARTIFACT {key}: {value}")
    return "\n".join(lines)


def _append_attempt_output(output_file: str | Path, text: str) -> None:
    """Persist every Test Expert try-out instead of truncating prior evidence."""
    path = Path(output_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_separator = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8") as handle:
        if needs_separator:
            handle.write("\n")
        handle.write(text)


def test_expert_node(state: MaintenanceWorkflowState) -> dict:
    """Validate one userspace-C contract in a fresh QEMU image copy."""
    set_session_dir(state.get("session_dir"))
    ensure_output_dir()
    output_file = get_expert_output_file("test_expert")
    tryout = int(state.get("tryout_count", 0) or 0) + 1
    maximum = int(state.get("max_tryouts", 10) or 10)
    previous_rounds = list(state.get("test_rounds", []) or [])
    contract_history = list(state.get("kernel_contract_history", []) or [])
    rootfs_mode = _configured_rootfs_mode(state.get("config") or {})
    consumes_loop = True
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
        elif (incremental_error := _validate_incremental_kernel_contract(contract, contract_history)):
            result = _blocked_attempt(
                code="BLOCKED_INVALID_INCREMENTAL_CONTRACT",
                summary=incremental_error,
                tryout=tryout,
            )
        elif (host_error := _host_qemu_capability_error(contract.target_arch)):
            result = _blocked_attempt(
                code="BLOCKED_ENVIRONMENT_CAPABILITY",
                summary=host_error,
                tryout=0,
            )
            consumes_loop = False
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
                        rootfs_mode=rootfs_mode,
                    )
                    image_artifacts["rootfs_mode"] = rootfs_mode
                    if contract.rootfs_path:
                        image_artifacts["declared_rootfs"] = contract.rootfs_path
                except (OSError, ValueError) as exc:
                    result = _blocked_attempt(code="BLOCKED_BASE_IMAGE_MISSING", summary=str(exc), tryout=tryout)
                else:
                    result = run_persistent_qemu_test_plan(
                        plan, attempt=tryout, runtime_root=runtime_root,
                    )
                    result.artifacts.update(image_artifacts)
                    result = _promote_guest_capability_block(result)
                    result = _apply_reproducer_regression_guard(
                        result, previous_rounds,
                    )
                    result.call_chain_consistent = bool(result.test_passed)
                    result.principle_consistent, result.semantic_review_reason = _semantic_review(contract, result)
                    result.test_passed = bool(result.call_chain_consistent and result.principle_consistent)
                    if result.call_chain_consistent and not result.principle_consistent:
                        result.status = "failed"
                        result.code = "FAILED_SEMANTIC_CALL_CHAIN_REVIEW"
                        result.summary = result.semantic_review_reason
                    result = _apply_progress_metadata(result, contract, previous_rounds)
                    if not result.test_passed and result.status not in {"blocked", "skipped"}:
                        if tryout >= maximum:
                            result.code = "FAILED_CALL_CHAIN_MISMATCH_AFTER_10_TRYOUTS"
                        elif not result.kernel_feedback:
                            result.kernel_feedback = "Review missing/reordered frames and revise the userspace trigger or declared injection plan."
                    result.kernel_feedback = _augment_kernel_feedback(result, previous_rounds)

    text = _format_attempt(result)
    _append_attempt_output(
        output_file,
        _format_agent_header_text("测试专家", f"调用链验证 - 第{tryout}次")
        + text
        + "\n"
        + _format_agent_footer_text("测试专家"),
    )
    result_data = model_to_dict(result)
    previous_rounds.append(result_data)
    effective_tryout = tryout if consumes_loop else int(state.get("tryout_count", 0) or 0)
    return {
        "test_result": text,
        "test_passed": result.test_passed,
        "test_attempts": effective_tryout,
        "tryout_count": effective_tryout,
        "test_rounds": previous_rounds,
        "test_contract": result_data,
        "test_attempt_contract": result_data,
        "call_chain_consistent": result.call_chain_consistent and result.principle_consistent,
        "test_feedback": result.kernel_feedback,
        "final_response": text if result.status in {"blocked", "skipped"} or tryout >= maximum else "",
    }
