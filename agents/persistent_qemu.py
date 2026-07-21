"""Persistent QEMU guest lifecycle and SSH-based reproducer execution.

This module is deliberately independent of the legacy one-shot initramfs
runner.  A guest is identified by the exact boot kernel, rootfs, architecture,
and QEMU recipe.  It is reused only while that identity is unchanged; a new
kernel can therefore never inherit a previous case's guest state.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import time
import re
from typing import Any
from uuid import uuid4

from agents.contracts import QemuRecipe, TestPlan, TestResultContract, ToolStepResult
from agents.qemu_tools import _select_qemu_memory
from agents.test_runner import _check_causal_reproduction, _match_serial_signals


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGE_ROOT = PROJECT_ROOT / "runtime" / "qemu-ssh"
_SAFE_PAYLOAD_PATH = re.compile(r"^(?:modules|bin)/[A-Za-z0-9][A-Za-z0-9._+-]*$")
_SAFE_SYSCTL_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_PRESSURE_PROFILES = {"cpu": "--cpu", "memory": "--vm", "io": "--io",
                      "scheduler": "--switch", "filesystem": "--hdd"}


@dataclass(frozen=True)
class PersistentQemuPaths:
    """All paths belonging to a persistent guest architecture."""

    arch: str
    image: Path
    ssh_key: Path
    runtime_dir: Path

    @property
    def state_file(self) -> Path:
        return self.runtime_dir / "state.json"

    @property
    def serial_log(self) -> Path:
        return self.runtime_dir / "serial.log"

    @property
    def qemu_log(self) -> Path:
        return self.runtime_dir / "qemu.log"


def persistent_qemu_paths(arch: str, *, runtime_root: Path | None = None) -> PersistentQemuPaths:
    normalized = _normalize_arch(arch)
    root = runtime_root or DEFAULT_IMAGE_ROOT
    arch_root = root / normalized
    return PersistentQemuPaths(
        arch=normalized,
        image=arch_root / "debian.img",
        ssh_key=arch_root / "lumen_qemu_ed25519",
        runtime_dir=arch_root / "runtime",
    )


def _normalize_arch(arch: str) -> str:
    aliases = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}
    return aliases.get((arch or "").strip().lower(), (arch or "").strip().lower())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def guest_identity(plan: TestPlan, paths: PersistentQemuPaths) -> dict[str, Any]:
    """Return the immutable compatibility identity used for VM reuse."""
    kernel = Path(os.path.expanduser(plan.boot_kernel_path)).resolve()
    return {
        "schema": 1,
        "arch": paths.arch,
        "kernel_path": str(kernel),
        "kernel_sha256": _sha256_file(kernel),
        "rootfs_path": str(paths.image.resolve()),
        "rootfs_sha256": _sha256_file(paths.image),
        "recipe": {
            "machine": plan.qemu_recipe.machine,
            "cpu": plan.qemu_recipe.cpu,
            "smp": plan.qemu_recipe.smp,
            "memory": plan.qemu_recipe.memory,
            "extra_cmdline": plan.qemu_recipe.extra_cmdline,
        },
    }


def _pid_is_live(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _host_arch() -> str:
    return _normalize_arch(os.uname().machine)


def _validate_execution_steps(plan: TestPlan) -> None:
    """Reject unspecified or unsafe guest actions before QEMU is touched."""
    if not plan.execution_steps:
        raise ValueError("execution_steps must not be empty")
    for index, step in enumerate(plan.execution_steps, start=1):
        if step.type in {"load_module", "run_binary"}:
            expected_root = "modules" if step.type == "load_module" else "bin"
            if not _SAFE_PAYLOAD_PATH.fullmatch(step.path) or not step.path.startswith(expected_root + "/"):
                raise ValueError(f"execution step {index} has invalid {step.type} path: {step.path!r}")
            if any("\x00" in arg or "\n" in arg for arg in step.args):
                raise ValueError(f"execution step {index} has unsafe arguments")
        elif step.type == "run_pressure":
            if step.profile not in _PRESSURE_PROFILES:
                raise ValueError(f"execution step {index} has invalid pressure profile: {step.profile!r}")
            if not 1 <= step.workers <= 32:
                raise ValueError(f"execution step {index} pressure workers must be in 1..32")
            if not 1 <= step.seconds <= 300:
                raise ValueError(f"execution step {index} pressure seconds must be in 1..300")
        elif step.type == "write_sysctl":
            if not _SAFE_SYSCTL_KEY.fullmatch(step.key) or ".." in step.key or not step.value:
                raise ValueError(f"execution step {index} has invalid sysctl declaration")
        elif step.type == "wait":
            if not 1 <= step.seconds <= 300:
                raise ValueError(f"execution step {index} wait seconds must be in 1..300")


def _render_execution_script(plan: TestPlan, marker: str) -> str:
    """Render runner-owned POSIX shell from allow-listed structured steps."""
    _validate_execution_steps(plan)
    lines = [
        "#!/bin/sh", "set -eu", "PRESSURE_PIDS=",
        "trap '[ -z \"${PRESSURE_PIDS:-}\" ] || kill $PRESSURE_PIDS 2>/dev/null || true' EXIT",
        f"echo {shlex.quote(marker)} > /dev/console", "cd /tmp/lumen-poc",
    ]
    for step in plan.execution_steps:
        if step.type == "load_module":
            lines.append(f"test -f {shlex.quote('./' + step.path)}")
            lines.append(f"insmod {shlex.quote('./' + step.path)}")
        elif step.type == "run_binary":
            command = " ".join([shlex.quote("./" + step.path), *(shlex.quote(arg) for arg in step.args)])
            lines.append(f"test -x {shlex.quote('./' + step.path)}")
            lines.append(f"timeout --signal=KILL 300 {command}")
        elif step.type == "run_pressure":
            pressure_args = ["stress-ng", _PRESSURE_PROFILES[step.profile], str(step.workers)]
            if step.profile == "memory":
                pressure_args += ["--vm-bytes", "75%"]
            elif step.profile == "filesystem":
                pressure_args += ["--hdd-bytes", "64M"]
            pressure_args += ["--timeout", f"{step.seconds}s", "--metrics-brief"]
            command = " ".join(shlex.quote(arg) for arg in pressure_args)
            lines.append(f"{command} >/dev/console 2>&1 &")
            lines.append("PRESSURE_PIDS=\"${PRESSURE_PIDS:-} $!\"")
        elif step.type == "write_sysctl":
            sysctl_path = "/proc/sys/" + step.key.replace(".", "/")
            lines.append(f"printf '%s\\n' {shlex.quote(step.value)} > {shlex.quote(sysctl_path)}")
        else:  # validated Literal leaves only wait
            lines.append(f"sleep {step.seconds}")
    return "\n".join(lines) + "\n"


def build_qemu_command(plan: TestPlan, paths: PersistentQemuPaths, *, ssh_port: int) -> tuple[list[str], str]:
    """Build a deterministic, SSH-enabled QEMU command without launching it."""
    arch = paths.arch
    recipe = plan.qemu_recipe
    memory = recipe.memory or _select_qemu_memory(plan.boot_kernel_path, "")
    smp = recipe.smp or "2"
    host_matches_target = _host_arch() == arch
    kvm_available = host_matches_target and os.access("/dev/kvm", os.R_OK | os.W_OK)

    if arch == "x86_64":
        qemu = "qemu-system-x86_64"
        machine = recipe.machine or ("q35,accel=kvm:tcg" if kvm_available else "q35,accel=tcg")
        cpu = recipe.cpu or ("host" if kvm_available else "max")
        root_device = "/dev/sda"
        console = "ttyS0"
        net_device = "e1000,netdev=net0"
        drive_args = ["-drive", f"file={paths.image},format=raw,if=ide"]
    elif arch == "arm64":
        qemu = "qemu-system-aarch64"
        machine = recipe.machine or ("virt,accel=kvm:tcg" if kvm_available else "virt,accel=tcg")
        cpu = recipe.cpu or ("host" if kvm_available else "cortex-a57")
        root_device = "/dev/vda"
        console = "ttyAMA0"
        net_device = "virtio-net-device,netdev=net0"
        drive_args = [
            "-drive", f"if=none,id=rootfs,file={paths.image},format=raw",
            "-device", "virtio-blk-device,drive=rootfs",
        ]
    else:
        raise ValueError(f"unsupported persistent QEMU architecture: {arch}")

    # Both documented x86 and arm64 debug boots require early serial output.
    cmdline = f"console={console} root={root_device} rw net.ifnames=0 earlyprintk=serial"
    if recipe.extra_cmdline:
        cmdline += " " + recipe.extra_cmdline
    command = [
        qemu,
        "-machine", machine,
        "-cpu", cpu,
        "-smp", smp,
        "-m", memory,
        "-display", "none",
        "-monitor", "none",
        "-serial", f"file:{paths.serial_log}",
        "-no-reboot",
        "-kernel", os.path.expanduser(plan.boot_kernel_path),
        "-append", cmdline,
        *drive_args,
        "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{ssh_port}-:22",
        "-device", net_device,
    ]
    return command, ("kvm" if kvm_available else "tcg")


class PersistentQemuManager:
    """Own one architecture/kernel-specific QEMU guest and run a POC via SSH."""

    def __init__(self, plan: TestPlan, *, runtime_root: Path | None = None, boot_timeout: int = 120):
        self.plan = plan
        self.paths = persistent_qemu_paths(plan.target_arch, runtime_root=runtime_root)
        self.boot_timeout = boot_timeout

    def _ssh_base(self, port: int) -> list[str]:
        return [
            "ssh", "-i", str(self.paths.ssh_key), "-p", str(port),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=5", "root@127.0.0.1",
        ]

    def _ssh_ready(self, port: int) -> bool:
        result = subprocess.run(
            [*self._ssh_base(port), "true"], capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0

    def ensure_running(self) -> tuple[ToolStepResult, dict[str, Any]]:
        """Reuse a healthy matching guest or launch exactly one compatible guest."""
        missing = [str(path) for path in (self.paths.image, self.paths.ssh_key) if not path.is_file()]
        if missing:
            return ToolStepResult(
                name="ensure_persistent_qemu", status="blocked",
                message="Persistent SSH QEMU image is not provisioned.",
                artifacts={"provision_command": "bash scripts/provision_qemu_ssh_image.sh --arch all"},
                error="missing: " + ", ".join(missing),
            ), {}
        if not os.access(self.paths.ssh_key, os.R_OK):
            return ToolStepResult(name="ensure_persistent_qemu", status="blocked", message="SSH private key is unreadable."), {}

        identity = guest_identity(self.plan, self.paths)
        state = _read_state(self.paths.state_file)
        if state.get("identity") == identity and _pid_is_live(int(state.get("pid", 0))):
            port = int(state.get("ssh_port", 0))
            if port and self._ssh_ready(port):
                return ToolStepResult(
                    name="ensure_persistent_qemu", status="ok", message="Reused healthy persistent QEMU guest.",
                    artifacts={"serial_log": str(self.paths.serial_log), "state": str(self.paths.state_file)},
                ), state
            return ToolStepResult(
                name="ensure_persistent_qemu", status="failed",
                message="Matching QEMU process exists but SSH health check failed; explicit restart is required.",
                artifacts={"state": str(self.paths.state_file), "serial_log": str(self.paths.serial_log)},
            ), state

        if _pid_is_live(int(state.get("pid", 0))):
            # The manager owns only this architecture's recorded process.  A
            # different immutable identity cannot be reused, so rotate it
            # deterministically before launching the requested kernel.
            stopped = self.shutdown()
            if stopped.status != "ok":
                return ToolStepResult(
                    name="ensure_persistent_qemu", status="blocked",
                    message="A different persistent QEMU identity is running and did not stop cleanly.",
                    artifacts={"state": str(self.paths.state_file)},
                ), state

        self.paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        ssh_port = _reserve_local_port()
        command, acceleration = build_qemu_command(self.plan, self.paths, ssh_port=ssh_port)
        qemu_log = self.paths.qemu_log.open("a", encoding="utf-8")
        try:
            process = subprocess.Popen(
                command, stdout=qemu_log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        finally:
            qemu_log.close()
        state = {"pid": process.pid, "ssh_port": ssh_port, "identity": identity, "acceleration": acceleration, "command": command}
        _write_state(self.paths.state_file, state)
        deadline = time.monotonic() + self.boot_timeout
        while time.monotonic() < deadline:
            if not _pid_is_live(process.pid):
                return ToolStepResult(
                    name="ensure_persistent_qemu", status="failed", message="QEMU exited before SSH became ready.",
                    artifacts={"serial_log": str(self.paths.serial_log), "qemu_log": str(self.paths.qemu_log)},
                ), state
            if self._ssh_ready(ssh_port):
                return ToolStepResult(
                    name="ensure_persistent_qemu", status="ok", message="Started persistent QEMU guest and verified SSH.",
                    artifacts={"serial_log": str(self.paths.serial_log), "state": str(self.paths.state_file), "qemu_log": str(self.paths.qemu_log)},
                ), state
            time.sleep(1)
        return ToolStepResult(
            name="ensure_persistent_qemu", status="failed", message="QEMU boot timed out before SSH became ready.",
            artifacts={"serial_log": str(self.paths.serial_log), "qemu_log": str(self.paths.qemu_log)},
        ), state

    def shutdown(self) -> ToolStepResult:
        state = _read_state(self.paths.state_file)
        pid = int(state.get("pid", 0))
        if not _pid_is_live(pid):
            return ToolStepResult(name="shutdown_persistent_qemu", status="ok", message="No live QEMU process.")
        os.kill(pid, 15)
        deadline = time.monotonic() + 10
        while _pid_is_live(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if _pid_is_live(pid):
            return ToolStepResult(name="shutdown_persistent_qemu", status="failed", message="QEMU did not exit after SIGTERM.")
        return ToolStepResult(name="shutdown_persistent_qemu", status="ok", message="Persistent QEMU stopped.")

    def _stage_poc(self) -> tuple[Path, str]:
        if not self.plan.execution_steps:
            raise ValueError("execution_steps must not be empty")
        stage = self.paths.runtime_dir / "poc" / uuid4().hex
        stage.mkdir(parents=True, exist_ok=False)
        if self.plan.reproducer_dir:
            source_dir = Path(os.path.expanduser(self.plan.reproducer_dir)).resolve()
            if source_dir.is_dir():
                shutil.copytree(source_dir, stage / "reproducer", dirs_exist_ok=True)
        if self.plan.reproducer_module_path:
            module = Path(os.path.expanduser(self.plan.reproducer_module_path)).resolve()
            if module.is_file():
                modules = stage / "modules"
                modules.mkdir(exist_ok=True)
                shutil.copy2(module, modules / module.name)
        if self.plan.binaries_dir:
            binaries = Path(os.path.expanduser(self.plan.binaries_dir)).resolve()
            if binaries.is_dir():
                shutil.copytree(binaries, stage / "bin", dirs_exist_ok=True)
        case_id = self.plan.reproduction_case_id or "untracked"
        path_id = self.plan.target_path_id or "untracked"
        marker = f"LUMEN_REPRO_START:{case_id}:{path_id}"
        (stage / "run.sh").write_text(_render_execution_script(self.plan, marker), encoding="utf-8")
        os.chmod(stage / "run.sh", 0o700)
        return stage, marker

    def run_poc(self, state: dict[str, Any]) -> ToolStepResult:
        """Upload an isolated POC directory, execute it, and retain raw evidence."""
        port = int(state.get("ssh_port", 0))
        if not port:
            return ToolStepResult(name="run_poc_over_ssh", status="blocked", message="Persistent QEMU state has no SSH port.")
        stage, _ = self._stage_poc()
        remote = "/tmp/lumen-poc"
        serial_offset = self.paths.serial_log.stat().st_size if self.paths.serial_log.exists() else 0
        command = "rm -rf /tmp/lumen-poc && mkdir -p /tmp/lumen-poc"
        mkdir_result = subprocess.run([*self._ssh_base(port), command], capture_output=True, text=True, timeout=15)
        if mkdir_result.returncode != 0:
            return ToolStepResult(name="run_poc_over_ssh", status="failed", message="Failed to prepare remote POC directory.", error=mkdir_result.stderr[-1000:])
        upload = subprocess.run(
            ["scp", "-i", str(self.paths.ssh_key), "-P", str(port), "-r",
             "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null", f"{stage}/.", f"root@127.0.0.1:{remote}"],
            capture_output=True, text=True, timeout=60,
        )
        if upload.returncode != 0:
            return ToolStepResult(name="run_poc_over_ssh", status="failed", message="Failed to upload POC over SSH.", error=upload.stderr[-1000:])
        executed = subprocess.run([*self._ssh_base(port), "sh /tmp/lumen-poc/run.sh"], capture_output=True, text=True, timeout=330)
        output_file = stage / "ssh-command.log"
        output_file.write_text(executed.stdout + "\n--- stderr ---\n" + executed.stderr, encoding="utf-8")
        state["serial_offset"] = serial_offset
        _write_state(self.paths.state_file, state)
        return ToolStepResult(
            name="run_poc_over_ssh", status="ok" if executed.returncode == 0 else "failed",
            message="POC command completed over SSH." if executed.returncode == 0 else "POC command ended abnormally; serial evidence will decide reproduction.",
            artifacts={"poc_stage": str(stage), "ssh_output": str(output_file), "serial_log": str(self.paths.serial_log)},
            output=executed.stdout[-4000:], error=executed.stderr[-4000:],
        )


def run_persistent_qemu_test_plan(plan: TestPlan, *, attempt: int, runtime_root: Path | None = None) -> TestResultContract:
    """Run one POC against a reusable SSH QEMU guest and evaluate host serial evidence."""
    try:
        _validate_execution_steps(plan)
    except ValueError as exc:
        return TestResultContract(
            status="blocked", code="BLOCKED_EXECUTION_PLAN", attempts=attempt,
            summary=str(exc), plan=plan,
        )
    manager = PersistentQemuManager(plan, runtime_root=runtime_root)
    steps: list[ToolStepResult] = []
    ensure, state = manager.ensure_running()
    steps.append(ensure)
    if ensure.status != "ok":
        return TestResultContract(status="blocked" if ensure.status == "blocked" else "failed", code="BLOCKED_PERSISTENT_QEMU" if ensure.status == "blocked" else "FAILED_PERSISTENT_QEMU_BOOT", attempts=attempt, summary=ensure.message, plan=plan, steps=steps, artifacts=ensure.artifacts)
    try:
        execution = manager.run_poc(state)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        execution = ToolStepResult(name="run_poc_over_ssh", status="failed", message="POC execution setup failed.", error=str(exc))
    steps.append(execution)
    serial = manager.paths.serial_log
    content = serial.read_text(encoding="utf-8", errors="replace") if serial.exists() else ""
    offset = int(state.get("serial_offset", 0))
    content = content[offset:] if offset > 0 else content
    matched = _match_serial_signals(log_content=content, detection=plan.detection_signals, expected_signal=plan.expected_signal)
    artifacts = {artifact_key: artifact_path for step in steps for artifact_key, artifact_path in step.artifacts.items()}
    causal = _check_causal_reproduction(content, plan, matched) if matched else {}
    if matched and (not plan.require_causal_reproduction or all(causal.get(field) for field in ("reproducer_started", "signal_after_start", "target_context_matched"))):
        return TestResultContract(status="ok", code="PASSED_REPRODUCED", test_passed=True, attempts=attempt, summary=f"Expected signal observed after SSH POC start: {matched}", plan=plan, steps=steps, artifacts=artifacts, target_path_id=plan.target_path_id, **causal)
    if matched:
        return TestResultContract(status="failed", code="FAILED_CAUSAL_REPRODUCTION", attempts=attempt, summary="Signal was observed but its causal POC proof is incomplete.", plan=plan, steps=steps, artifacts=artifacts, target_path_id=plan.target_path_id, **causal)
    return TestResultContract(status="failed", code="FAILED_SIGNAL_NOT_FOUND", attempts=attempt, summary="No expected signal was observed in the serial log after the SSH POC started.", plan=plan, steps=steps, artifacts=artifacts)
