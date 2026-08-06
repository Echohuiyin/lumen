"""Persistent QEMU guest lifecycle and SSH-based reproducer execution.

A guest is identified by the exact boot kernel, disk image, architecture,
and QEMU recipe.  It is reused only while that identity is unchanged; a new
kernel can therefore never inherit a previous case's guest state.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import lzma
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import time
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from uuid import uuid4

from agents.contracts import QemuRecipe, TestPlan, TestResultContract, ToolStepResult
from agents.qemu_tools import _select_qemu_memory
from agents.test_runner import _check_causal_reproduction, _match_serial_signals


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGE_ROOT = PROJECT_ROOT / "runtime" / "qemu-ssh"
_SAFE_PAYLOAD_PATH = re.compile(r"^bin/[A-Za-z0-9][A-Za-z0-9._+-]*$")
_SAFE_INTERFACE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
_SAFE_SYSCTL_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SAFE_GUEST_WORKDIR = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SAFE_SSH_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*\$?$")


def _configured_image_root() -> Path:
    """Resolve the deployment-selected persistent QEMU image root."""
    configured = os.environ.get("LUMEN_QEMU_IMAGE_ROOT", "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_IMAGE_ROOT


def _guest_poc_root() -> str:
    """Resolve and validate the guest-side scratch directory."""
    configured = os.environ.get("LUMEN_QEMU_GUEST_WORKDIR", "").strip() or "/tmp/lumen-poc"
    if (
        configured == "/"
        or ".." in Path(configured).parts
        or not _SAFE_GUEST_WORKDIR.fullmatch(configured)
    ):
        raise ValueError(
            "LUMEN_QEMU_GUEST_WORKDIR must be an absolute path without '..' or shell metacharacters"
        )
    return configured.rstrip("/")


def _ssh_user() -> str:
    """Resolve the guest account used by the provisioned SSH key."""
    configured = os.environ.get("LUMEN_QEMU_SSH_USER", "").strip() or "root"
    if len(configured) > 64 or not _SAFE_SSH_USER.fullmatch(configured):
        raise ValueError("LUMEN_QEMU_SSH_USER is not a valid SSH account name")
    return configured


def _resolve_qemu_kernel(boot_kernel_path: str, runtime_dir: Path) -> str:
    """Return a kernel image format that QEMU can load directly.

    Case inputs may point at the compressed ``*.xz`` transport artifact.  The
    ARM64 QEMU loader does not recursively unpack that container, so passing
    it to ``-kernel`` leaves a live but unbootable VM with no serial output.
    Prefer the sibling image produced by the same asset bundle (usually the
    inner gzip/raw image); when it is not present, decompress into the
    try-out's private runtime directory and keep the declared artifact read
    only.
    """
    kernel = Path(os.path.expanduser(boot_kernel_path)).resolve()
    if kernel.suffix.lower() != ".xz":
        return str(kernel)
    if not kernel.is_file():
        raise FileNotFoundError(f"boot kernel is missing: {kernel}")

    sibling = kernel.with_suffix("")
    if sibling.is_file():
        return str(sibling)

    stat = kernel.stat()
    digest = hashlib.sha256(
        f"{kernel}\0{stat.st_size}\0{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:16]
    output_suffix = sibling.suffix or ".img"
    output = runtime_dir / f"kernel-{digest}{output_suffix}"
    if output.is_file() and output.stat().st_mtime_ns >= stat.st_mtime_ns:
        return str(output)

    runtime_dir.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    try:
        with lzma.open(kernel, "rb") as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target)
        os.replace(temporary, output)
        output.chmod(0o644)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return str(output)


_PRESSURE_PROFILES = {"cpu": "--cpu", "memory": "--vm", "io": "--io",
                      "scheduler": "--switch", "filesystem": "--hdd", "network": "--netdev"}
_FAULT_PROFILES = {"failslab", "fail_page_alloc", "fail_futex", "fail_function", "fail_make_request"}
_DEFAULT_BOOT_TIMEOUT_SEC = 900
_DEFAULT_REPRODUCER_TIMEOUT_SEC = 60
_DEFAULT_SSH_EXECUTION_GRACE_SEC = 30
_DEFAULT_CALL_CHAIN_CAPTURE_GRACE_SEC = 5
_MAX_REPRODUCER_TIMEOUT_SEC = 7200
_MAX_SSH_EXECUTION_GRACE_SEC = 600
_MAX_CALL_CHAIN_CAPTURE_GRACE_SEC = 60
_MAX_CONCURRENT_INSTANCES = 16
_TERMINAL_BOOT_MARKERS = (
    "Kernel panic - not syncing:",
    "Attempted to kill init!",
    "Unable to mount root fs",
    "VFS: Cannot open root device",
    "No working init found",
)


def _terminal_boot_failure(serial_log: Path) -> str:
    """Return a terminal boot marker, if the guest can no longer accept SSH."""
    try:
        content = serial_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # Keep the check bounded: boot logs can grow during long-running guests.
    tail = content[-256 * 1024:]
    for marker in _TERMINAL_BOOT_MARKERS:
        if marker in tail:
            return marker
    return ""


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
    root = runtime_root if runtime_root is not None else _configured_image_root()
    arch_root = root / normalized
    raw_image = arch_root / "debian.img"
    overlay_image = arch_root / "debian.qcow2"
    image = overlay_image if overlay_image.is_file() else raw_image
    return PersistentQemuPaths(
        arch=normalized,
        image=image,
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


def _root_device_for_image(image: Path, base_device: str) -> str:
    """Select the first partition when a raw QEMU image has a partition table.

    The provisioned Debian image is commonly a filesystem directly on the
    block device, while report-time syzbot disks are partitioned (for example
    ``vda1``).  Inspecting the image on the host keeps the kernel command line
    deterministic and avoids silently booting an unmountable root device.
    """
    probe_image = image
    if image.suffix.lower() in {".qcow2", ".qcow"}:
        backing_hint = image.with_suffix(".backing")
        if backing_hint.is_file():
            probe_image = backing_hint.resolve()
    try:
        result = subprocess.run(
            ["fdisk", "-l", str(probe_image)], capture_output=True, text=True,
            timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return base_device
    if result.returncode != 0:
        return base_device
    prefix = re.escape(str(image))
    if re.search(rf"(?m)^{prefix}\d+\s+", result.stdout or ""):
        return base_device + "1"
    return base_device


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
    # ``kill(pid, 0)`` also succeeds for an unreaped child in zombie state.
    # It is no longer a running QEMU and must not make a completed shutdown
    # look like a leaked guest.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        state = stat.rsplit(")", 1)[-1].lstrip()[:1]
        if state == "Z":
            return False
    except OSError:
        pass
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


def _configured_qemu_smp() -> str:
    """Resolve an explicit deployment default for kernels needing one vCPU."""
    configured = os.environ.get("LUMEN_QEMU_DEFAULT_SMP", "").strip()
    if not configured:
        return "2"
    if not configured.isdigit() or not 1 <= int(configured) <= 128:
        raise ValueError("LUMEN_QEMU_DEFAULT_SMP must be an integer in 1..128")
    return configured


def _validate_execution_steps(plan: TestPlan) -> None:
    """Reject unspecified or unsafe guest actions before QEMU is touched."""
    if not plan.execution_steps:
        raise ValueError("execution_steps must not be empty")
    for index, step in enumerate(plan.execution_steps, start=1):
        if step.type == "setup_vcan":
            if not _SAFE_INTERFACE.fullmatch(step.interface):
                raise ValueError(
                    f"execution step {index} has invalid vcan interface: {step.interface!r}"
                )
        elif step.type == "run_binary":
            if not _SAFE_PAYLOAD_PATH.fullmatch(step.path):
                raise ValueError(f"execution step {index} has invalid userspace binary path: {step.path!r}")
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
        elif step.type == "fault_injection":
            if step.profile not in _FAULT_PROFILES:
                raise ValueError(f"execution step {index} has invalid fault-injection profile: {step.profile!r}")
            if not 0 <= step.probability <= 100:
                raise ValueError(f"execution step {index} fault probability must be in 0..100")
            if not 1 <= step.interval <= 100000 or not 1 <= step.times <= 100000:
                raise ValueError(f"execution step {index} fault interval/times are out of range")


def _render_execution_script(plan: TestPlan, marker: str) -> str:
    """Render runner-owned POSIX shell from allow-listed structured steps."""
    _validate_execution_steps(plan)
    guest_root = _guest_poc_root()
    guest_bin = f"{guest_root}/bin"
    guest_reproducer = f"{guest_root}/reproducer"
    lines = [
        "#!/bin/sh", "set -eu", "PRESSURE_PIDS=",
        "trap '[ -z \"${PRESSURE_PIDS:-}\" ] || kill $PRESSURE_PIDS 2>/dev/null || true' EXIT",
        f"mkdir -p {shlex.quote(guest_bin)}",
        f"cd {shlex.quote(guest_reproducer)}",
    ]
    reproducer = plan.reproducer
    if reproducer.language != "c" or reproducer.artifact_type != "userspace":
        raise ValueError("only userspace C reproducers may be compiled in the guest")
    if not reproducer.source_files or reproducer.entry_source not in reproducer.source_files:
        raise ValueError("userspace reproducer requires declared source files and entry source")
    for source in reproducer.source_files:
        if not source or Path(source).is_absolute() or ".." in Path(source).parts or Path(source).suffix not in {".c", ".h"}:
            raise ValueError(f"invalid userspace C source: {source!r}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", reproducer.output_binary):
        raise ValueError("invalid userspace output binary name")
    compiler_args = [arg for arg in reproducer.compiler_args if "\n" not in arg and "\x00" not in arg]
    if len(compiler_args) != len(reproducer.compiler_args):
        raise ValueError("unsafe compiler argument")
    c_sources = [source for source in reproducer.source_files if source.endswith(".c")]
    if not c_sources:
        raise ValueError("userspace reproducer requires at least one C translation unit")
    pthread_pattern = re.compile(
        r"#\s*include\s*[<\"]pthread\.h[>\"]|"
        r"\bpthread_(?:create|join|barrier_|mutex_|cond_)"
    )
    uses_pthread = False
    source_root = Path(reproducer.source_dir)
    for source in c_sources:
        try:
            source_text = (source_root / source).read_text(encoding="utf-8", errors="replace")
        except OSError:
            source_text = ""
        if pthread_pattern.search(source_text):
            uses_pthread = True
            break
    libraries = [lib if lib.startswith("-l") else f"-l{lib}" for lib in reproducer.link_libraries]
    compile_command = " ".join([
        shlex.quote(reproducer.compiler), *(shlex.quote(source) for source in reproducer.source_files
        if source.endswith(".c")), *(shlex.quote(arg) for arg in compiler_args),
        *(shlex.quote(lib) for lib in libraries), "-o", shlex.quote("../bin/" + reproducer.output_binary),
    ])
    runtime_timeout = _normalise_reproducer_timeout(reproducer.runtime_timeout_sec)
    component_marker = f"LUMEN_GUEST_COMPONENT_MISSING:compiler:{reproducer.compiler}"
    lines.extend([
        f"if ! command -v {shlex.quote(reproducer.compiler)} >/dev/null 2>&1; then printf '%s\\n' {shlex.quote(component_marker)} > /dev/console 2>/dev/null || true; printf '%s\\n' {shlex.quote(component_marker)} >&2; exit 125; fi",
        compile_command,
    ])
    if uses_pthread:
        probe_source = [
            "#define _GNU_SOURCE",
            "#include <pthread.h>",
            "static void *lumen_probe_worker(void *opaque) {",
            "    (void)opaque;",
            "    return 0;",
            "}",
            "int main(void) {",
            "    pthread_t thread;",
            "    int rc = pthread_create(&thread, 0, lumen_probe_worker, 0);",
            "    if (rc != 0) return rc;",
            "    return pthread_join(thread, 0) != 0;",
            "}",
        ]
        probe_path = f"{guest_reproducer}/.lumen-pthread-probe.c"
        probe_binary = f"{guest_bin}/.lumen-pthread-probe"
        probe_error = f"{guest_root}/.lumen-pthread-probe.err"
        probe_component_marker = "LUMEN_GUEST_COMPONENT_MISSING:pthread:headers"
        probe_runtime_marker = "LUMEN_GUEST_RUNTIME_INCOMPATIBLE:pthread_clone"
        probe_lines = [
            "if ! command -v grep >/dev/null 2>&1; then",
            "    printf '%s\\n' LUMEN_GUEST_COMPONENT_MISSING:grep:runtime > /dev/console 2>/dev/null || true",
            "    printf '%s\\n' LUMEN_GUEST_COMPONENT_MISSING:grep:runtime >&2",
            "    exit 125",
            "fi",
            "if grep -Eq "
            + shlex.quote(r"#\s*include\s*[<\"]pthread\.h[>\"]|\bpthread_(create|join|barrier_|mutex_|cond_)")
            + " -- "
            + " ".join(shlex.quote(source) for source in c_sources)
            + "; then",
        ]
        probe_lines.extend(
            f"    printf '%s\\n' {shlex.quote(line)} >> {shlex.quote(probe_path)}"
            for line in probe_source
        )
        probe_compile = (
            f"{shlex.quote(reproducer.compiler)} -std=gnu11 -O2 -Wall -Wextra -Werror "
            f"-pthread {shlex.quote(probe_path)} -o {shlex.quote(probe_binary)}"
        )
        probe_lines.extend([
            f"    if ! {probe_compile}; then",
            f"        printf '%s\\n' {shlex.quote(probe_component_marker)} > /dev/console 2>/dev/null || true",
            f"        printf '%s\\n' {shlex.quote(probe_component_marker)} >&2",
            "        exit 125",
            "    fi",
            f"    if ! timeout --signal=KILL 5 {shlex.quote(probe_binary)} > {shlex.quote(probe_error)} 2>&1; then",
            f"        printf '%s\\n' {shlex.quote(probe_runtime_marker)} > /dev/console 2>/dev/null || true",
            f"        printf '%s\\n' {shlex.quote(probe_runtime_marker)} >&2",
            f"        cat {shlex.quote(probe_error)} >&2 || true",
            "        exit 125",
            "    fi",
            "fi",
        ])
        lines.extend(probe_lines)
    lines.extend([
        f"cd {shlex.quote(guest_root)}",
        f"echo {shlex.quote(marker)} > /dev/console",
    ])
    for step in plan.execution_steps:
        if step.type == "setup_vcan":
            interface = shlex.quote(step.interface)
            marker_name = re.sub(r"[^A-Za-z0-9_.+-]", "_", step.interface)
            component_marker = f"LUMEN_GUEST_COMPONENT_MISSING:kernel:vcan:{marker_name}"
            setup_marker = f"LUMEN_SETUP_VCAN interface={step.interface} result=ok"
            lines.extend([
                "if ! command -v ip >/dev/null 2>&1; then",
                "    printf '%s\\n' LUMEN_GUEST_COMPONENT_MISSING:ip:ip > /dev/console 2>/dev/null || true",
                "    printf '%s\\n' LUMEN_GUEST_COMPONENT_MISSING:ip:ip >&2",
                "    exit 125",
                "fi",
                f"if ! ip link show {interface} >/dev/null 2>&1; then",
                f"    if ! ip link add {interface} type vcan >/dev/null 2>&1; then",
                f"        printf '%s\\n' {shlex.quote(component_marker)} > /dev/console 2>/dev/null || true",
                f"        printf '%s\\n' {shlex.quote(component_marker)} >&2",
                "        exit 125",
                "    fi",
                "fi",
                f"if ! ip link set {interface} up >/dev/null 2>&1; then",
                f"    printf '%s\\n' {shlex.quote(component_marker)} > /dev/console 2>/dev/null || true",
                f"    printf '%s\\n' {shlex.quote(component_marker)} >&2",
                "    exit 125",
                "fi",
                f"printf '%s\\n' {shlex.quote(setup_marker)} > /dev/console 2>/dev/null || true",
            ])
        elif step.type == "run_binary":
            command = " ".join([shlex.quote("./" + step.path), *(shlex.quote(arg) for arg in step.args)])
            lines.append(f"test -x {shlex.quote('./' + step.path)}")
            lines.append(f"timeout --signal=KILL {runtime_timeout} {command}")
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
        elif step.type == "fault_injection":
            fault_dir = "/sys/kernel/debug/" + step.profile
            # Fault injection is an explicit part of the causal contract.  If
            # the guest kernel/debugfs cannot expose a writable control plane,
            # emit a structured capability marker instead of allowing the
            # shell's raw permission error to look like an ordinary mismatch.
            marker_target = re.sub(r"[^A-Za-z0-9_.+-]", "_", step.target or "unfiltered")[:96]
            fault_marker = f"LUMEN_GUEST_FAULT_INJECTION_UNAVAILABLE:{step.profile}:{marker_target}"
            lines.extend([
                f"if ! test -d {shlex.quote(fault_dir)}; then",
                f"    printf '%s\\n' {shlex.quote(fault_marker)} > /dev/console 2>/dev/null || true",
                f"    printf '%s\\n' {shlex.quote(fault_marker)} >&2",
                "    exit 125",
                "fi",
            ])
            fault_controls = [
                ("probability", str(step.probability)),
                ("interval", str(step.interval)),
                ("times", str(step.times)),
            ]
            if step.space:
                fault_controls.append(("space", str(step.space)))
            if step.target:
                fault_controls.append(("filter", step.target))
            for control, value in fault_controls:
                control_path = shlex.quote(fault_dir + "/" + control)
                lines.extend([
                    f"if ! printf '%s\\n' {shlex.quote(value)} > {control_path}; then",
                    f"    printf '%s\\n' {shlex.quote(fault_marker)} > /dev/console 2>/dev/null || true",
                    f"    printf '%s\\n' {shlex.quote(fault_marker)} >&2",
                    "    exit 125",
                    "fi",
                ])
        else:  # validated Literal leaves only wait
            lines.append(f"sleep {step.seconds}")
    return "\n".join(lines) + "\n"


def build_qemu_command(plan: TestPlan, paths: PersistentQemuPaths, *, ssh_port: int) -> tuple[list[str], str]:
    """Build a deterministic, SSH-enabled QEMU command without launching it."""
    arch = paths.arch
    recipe = plan.qemu_recipe
    memory = recipe.memory or _select_qemu_memory(plan.boot_kernel_path, "")
    smp = recipe.smp or _configured_qemu_smp()
    host_matches_target = _host_arch() == arch
    kvm_available = host_matches_target and os.access("/dev/kvm", os.R_OK | os.W_OK)

    image_format = "qcow2" if paths.image.suffix.lower() in {".qcow2", ".qcow"} else "raw"
    if arch == "x86_64":
        qemu = "qemu-system-x86_64"
        machine = recipe.machine or ("q35,accel=kvm:tcg" if kvm_available else "q35,accel=tcg")
        cpu = recipe.cpu or ("host" if kvm_available else "max")
        root_device = "/dev/sda"
        console = "ttyS0"
        net_device = "e1000,netdev=net0"
        drive_args = ["-drive", f"file={paths.image},format={image_format},if=ide"]
    elif arch == "arm64":
        qemu = "qemu-system-aarch64"
        machine = recipe.machine or ("virt,accel=kvm:tcg" if kvm_available else "virt,accel=tcg")
        cpu = recipe.cpu or ("host" if kvm_available else "cortex-a57")
        root_device = "/dev/vda"
        console = "ttyAMA0"
        # Use PCI virtio devices for the ARM guest.  The deployed ARM64
        # kernels commonly enable CONFIG_VIRTIO_PCI but not
        # CONFIG_VIRTIO_MMIO; the PCI form is also supported by QEMU's
        # `virt` machine and keeps the root disk discoverable as /dev/vda.
        net_device = "virtio-net-pci,netdev=net0"
        drive_args = [
            "-drive", f"if=none,id=rootfs,file={paths.image},format={image_format}",
            "-device", "virtio-blk-pci,drive=rootfs",
        ]
    else:
        raise ValueError(f"unsupported persistent QEMU architecture: {arch}")

    root_device = _root_device_for_image(paths.image, root_device)

    # Both documented x86 and arm64 debug boots require early serial output.
    cmdline = f"console={console} root={root_device} rw net.ifnames=0 earlyprintk=serial"
    if recipe.extra_cmdline:
        cmdline += " " + recipe.extra_cmdline
    qemu_kernel = _resolve_qemu_kernel(plan.boot_kernel_path, paths.runtime_dir)
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
        "-kernel", qemu_kernel,
        "-append", cmdline,
        *drive_args,
        "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{ssh_port}-:22",
        "-device", net_device,
    ]
    return command, ("kvm" if kvm_available else "tcg")


def _stage_declared_test_assets(plan: TestPlan, destination: Path) -> None:
    """Stage only input-declared fixture files referenced by guest arguments.

    ``input.txt`` paths are host paths; the guest cannot see them.  Resolve
    only files under the authoritative ``test_assets_dir`` and expose them as
    ``bin/assets/<relative>``.  Unrelated files (kernel images, vmlinux,
    disk.raw, etc.) are never copied into a try-out.
    """
    assets_value = str(getattr(plan, "test_assets_dir", "") or "").strip()
    if not assets_value:
        return
    asset_root = Path(os.path.expanduser(assets_value)).resolve()
    if not asset_root.is_dir():
        raise ValueError(f"declared test_assets_dir is missing: {asset_root}")

    copied: dict[str, str] = {}

    def normalize(args: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in args:
            value = str(raw)
            candidate: Path | None = None
            relative: Path | None = None
            path = Path(value)
            guest_asset_prefix = "/guest-assets/"
            if value.startswith(guest_asset_prefix):
                # Kernel Expert may describe a fixture using the stable guest
                # alias from the input contract.  Resolve the suffix against
                # the authoritative host asset root before staging it; never
                # treat an arbitrary guest path as a host path.
                alias_path = Path(value[len(guest_asset_prefix):])
                resolved = (asset_root / alias_path).resolve()
                try:
                    relative = resolved.relative_to(asset_root)
                except ValueError:
                    normalized.append(value)
                    continue
                candidate = resolved
            elif path.is_absolute():
                resolved = path.resolve()
                try:
                    relative = resolved.relative_to(asset_root)
                except ValueError:
                    # Absolute paths outside the declared fixture root may be
                    # legitimate guest paths (/proc, /tmp, ...).  They are
                    # intentionally left untouched; a host fixture path is
                    # only rewritten when it is proven to be under the root.
                    normalized.append(value)
                    continue
                candidate = resolved
            else:
                resolved = (asset_root / path).resolve()
                try:
                    relative = resolved.relative_to(asset_root)
                except ValueError:
                    normalized.append(value)
                    continue
                if not resolved.is_file():
                    normalized.append(value)
                    continue
                candidate = resolved
            if candidate is None or relative is None or not candidate.is_file():
                normalized.append(value)
                continue
            rel_text = relative.as_posix()
            if rel_text not in copied:
                target = destination / "assets" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, target)
                copied[rel_text] = str(target)
            normalized.append("bin/assets/" + rel_text)
        return normalized

    plan.reproducer.run_args = normalize(plan.reproducer.run_args)
    for step in plan.execution_steps:
        if step.type == "run_binary":
            step.args = normalize(step.args)


def _stage_declared_binaries(plan: TestPlan, source_root: Path, destination: Path) -> None:
    """Stage only prebuilt binaries named by the structured execution plan.

    binaries_dir may point at a benchmark asset bundle that also contains
    multi-gigabyte kernels, disk images, and debug symbols.  Copying that
    directory wholesale makes the SSH upload depend on unrelated host assets
    and can time out before the userspace reproducer starts.  The guest still
    compiles declared C sources; this helper only preserves an explicitly
    declared prebuilt payload when one exists.
    """
    declared: list[Path] = []
    for step in plan.execution_steps:
        if step.type != "run_binary" or not step.path.startswith("bin/"):
            continue
        relative = Path(step.path[len("bin/"):])
        if (
            not relative.name
            or relative.is_absolute()
            or ".." in relative.parts
            or len(relative.parts) != 1
        ):
            continue
        declared.append(relative)

    output_name = str(plan.reproducer.output_binary or "").strip()
    output = Path(output_name)
    if (
        output.name
        and not output.is_absolute()
        and ".." not in output.parts
        and len(output.parts) == 1
    ):
        declared.append(output)

    seen: set[str] = set()
    for relative in declared:
        key = relative.as_posix()
        if key in seen:
            continue
        seen.add(key)
        source = (source_root / relative).resolve()
        try:
            source.relative_to(source_root)
        except ValueError:
            continue
        if not source.is_file():
            # Missing prebuilt payloads are normal: the guest script compiles
            # the declared userspace C reproducer before executing it.
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


class PersistentQemuManager:
    """Own one architecture/kernel-specific QEMU guest and run a POC via SSH."""

    def __init__(self, plan: TestPlan, *, runtime_root: Path | None = None, boot_timeout: int | None = None):
        self.plan = plan
        self.paths = persistent_qemu_paths(plan.target_arch, runtime_root=runtime_root)
        requested_timeout = plan.qemu_recipe.timeout_sec if boot_timeout is None else boot_timeout
        self.boot_timeout = _normalise_boot_timeout(requested_timeout)
        self._process: subprocess.Popen | None = None

    def _ssh_base(self, port: int) -> list[str]:
        return [
            "ssh", "-i", str(self.paths.ssh_key), "-p", str(port),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=5", f"{_ssh_user()}@127.0.0.1",
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
        self._process = process
        state = {"pid": process.pid, "ssh_port": ssh_port, "identity": identity, "acceleration": acceleration, "command": command}
        _write_state(self.paths.state_file, state)
        deadline = time.monotonic() + self.boot_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._process = None
                return ToolStepResult(
                    name="ensure_persistent_qemu", status="failed", message="QEMU exited before SSH became ready.",
                    artifacts={"serial_log": str(self.paths.serial_log), "qemu_log": str(self.paths.qemu_log)},
                ), state
            terminal_marker = _terminal_boot_failure(self.paths.serial_log)
            if terminal_marker:
                stopped = self.shutdown()
                artifacts = {
                    "serial_log": str(self.paths.serial_log),
                    "qemu_log": str(self.paths.qemu_log),
                }
                if stopped.status != "ok":
                    artifacts["shutdown_error"] = stopped.message
                return ToolStepResult(
                    name="ensure_persistent_qemu",
                    status="blocked",
                    message=(
                        "QEMU reached a terminal boot failure before SSH became ready: "
                        f"{terminal_marker}"
                    ),
                    artifacts=artifacts,
                    error=terminal_marker,
                ), state
            if self._ssh_ready(ssh_port):
                return ToolStepResult(
                    name="ensure_persistent_qemu", status="ok", message="Started persistent QEMU guest and verified SSH.",
                    artifacts={"serial_log": str(self.paths.serial_log), "state": str(self.paths.state_file), "qemu_log": str(self.paths.qemu_log)},
                ), state
            time.sleep(1)
        # A boot timeout is a failed attempt, not a request to leave the
        # guest running in the background.  Reap this exact process before
        # returning so the next evidence-backed try-out starts with an
        # isolated VM and does not exhaust the host's KVM/ disk resources.
        stopped = self.shutdown()
        artifacts = {"serial_log": str(self.paths.serial_log), "qemu_log": str(self.paths.qemu_log)}
        if stopped.status != "ok":
            artifacts["shutdown_error"] = stopped.message
        return ToolStepResult(
            name="ensure_persistent_qemu", status="failed", message="QEMU boot timed out before SSH became ready.",
            artifacts=artifacts,
        ), state

    def shutdown(self) -> ToolStepResult:
        state = _read_state(self.paths.state_file)
        pid = int(state.get("pid", 0))
        owned = self._process if self._process is not None and self._process.pid == pid else None
        if owned is not None:
            if owned.poll() is not None:
                self._process = None
                return ToolStepResult(name="shutdown_persistent_qemu", status="ok", message="Persistent QEMU already exited.")
            owned.terminate()
            try:
                owned.wait(timeout=10)
                self._process = None
                return ToolStepResult(name="shutdown_persistent_qemu", status="ok", message="Persistent QEMU stopped.")
            except subprocess.TimeoutExpired:
                owned.kill()
                try:
                    owned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    return ToolStepResult(
                        name="shutdown_persistent_qemu", status="failed",
                        message="QEMU did not exit after SIGTERM or SIGKILL.",
                    )
                self._process = None
                return ToolStepResult(
                    name="shutdown_persistent_qemu", status="ok",
                    message="Persistent QEMU stopped after SIGKILL fallback.",
                )
        if not _pid_is_live(pid):
            return ToolStepResult(name="shutdown_persistent_qemu", status="ok", message="No live QEMU process.")
        os.kill(pid, 15)
        deadline = time.monotonic() + 10
        while _pid_is_live(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if _pid_is_live(pid):
            # A guest that is suspended or wedged in a KVM ioctl may not
            # service SIGTERM.  The manager owns this exact recorded PID, so
            # force-reap it before returning; leaving it alive would contaminate
            # the next isolated try-out and exhaust host KVM resources.
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            kill_deadline = time.monotonic() + 5
            while _pid_is_live(pid) and time.monotonic() < kill_deadline:
                time.sleep(0.2)
            if _pid_is_live(pid):
                return ToolStepResult(
                    name="shutdown_persistent_qemu", status="failed",
                    message="QEMU did not exit after SIGTERM or SIGKILL.",
                )
            return ToolStepResult(
                name="shutdown_persistent_qemu", status="ok",
                message="Persistent QEMU stopped after SIGKILL fallback.",
            )
        return ToolStepResult(name="shutdown_persistent_qemu", status="ok", message="Persistent QEMU stopped.")

    def _stage_poc(self) -> tuple[Path, str]:
        if not self.plan.execution_steps:
            raise ValueError("execution_steps must not be empty")
        stage = self.paths.runtime_dir / "poc" / uuid4().hex
        stage.mkdir(parents=True, exist_ok=False)
        if self.plan.reproducer_dir:
            source_dir = Path(os.path.expanduser(self.plan.reproducer_dir)).resolve()
            if not source_dir.is_dir():
                raise ValueError(f"reproducer source directory is missing: {source_dir}")
            # The Kernel Expert output directory is also the session root.  It
            # contains tryouts/ and prior runner artifacts, so copying the
            # whole directory would recursively copy the current QEMU stage
            # into itself.  Transfer only the declared C/H inputs.
            destination = stage / "reproducer"
            destination.mkdir(parents=True, exist_ok=True)
            declared_sources = list(self.plan.reproducer.source_files)
            for relative_name in declared_sources:
                source = (source_dir / relative_name).resolve()
                try:
                    source.relative_to(source_dir)
                except ValueError as exc:
                    raise ValueError(f"reproducer source escapes source directory: {relative_name!r}") from exc
                if not source.is_file():
                    raise ValueError(f"declared reproducer source is missing: {source}")
                shutil.copy2(source, destination / relative_name)
        if self.plan.reproducer_module_path:
            module = Path(os.path.expanduser(self.plan.reproducer_module_path)).resolve()
            if module.is_file():
                modules = stage / "modules"
                modules.mkdir(exist_ok=True)
                shutil.copy2(module, modules / module.name)
        if self.plan.binaries_dir:
            binaries = Path(os.path.expanduser(self.plan.binaries_dir)).resolve()
            if binaries.is_dir():
                _stage_declared_binaries(self.plan, binaries, stage / "bin")
        _stage_declared_test_assets(self.plan, stage / "bin")
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
        stage, marker = self._stage_poc()
        remote = _guest_poc_root()
        serial_offset = self.paths.serial_log.stat().st_size if self.paths.serial_log.exists() else 0
        quoted_remote = shlex.quote(remote)
        command = f"rm -rf {quoted_remote} && mkdir -p {quoted_remote}"
        mkdir_result = subprocess.run([*self._ssh_base(port), command], capture_output=True, text=True, timeout=15)
        if mkdir_result.returncode != 0:
            return ToolStepResult(name="run_poc_over_ssh", status="failed", message="Failed to prepare remote POC directory.", error=mkdir_result.stderr[-1000:])
        upload = subprocess.run(
            ["scp", "-i", str(self.paths.ssh_key), "-P", str(port), "-r",
             "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             f"{stage}/.", f"{_ssh_user()}@127.0.0.1:{remote}"],
            capture_output=True, text=True, timeout=60,
        )
        if upload.returncode != 0:
            return ToolStepResult(name="run_poc_over_ssh", status="failed", message="Failed to upload POC over SSH.", error=upload.stderr[-1000:])
        executed_proc = subprocess.Popen(
            [*self._ssh_base(port), f"sh {shlex.quote(remote + '/run.sh')}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + _normalise_reproducer_timeout(
            self.plan.reproducer.runtime_timeout_sec
        ) + _ssh_execution_grace_seconds()
        signal_seen = False
        signal_capture_deadline: float | None = None
        while executed_proc.poll() is None and time.monotonic() < deadline:
            if self.paths.serial_log.exists():
                serial_text = self.paths.serial_log.read_text(encoding="utf-8", errors="replace")
                # QEMU's serial file is appended asynchronously.  A byte-size
                # snapshot taken immediately before SSH starts can become
                # stale when buffered boot output is flushed afterwards, which
                # may place the marker before ``serial_offset``.  Anchor the
                # live polling window on the explicit marker whenever it is
                # visible; use the byte offset only before the marker appears.
                marker_index = serial_text.find(marker)
                post_marker = serial_text[marker_index:] if marker_index >= 0 else serial_text[serial_offset:]
                signal_seen = bool(_match_serial_signals(
                    log_content=post_marker,
                    detection=self.plan.detection_signals,
                    expected_signal=self.plan.expected_signal,
                ))
                if signal_seen:
                    if signal_capture_deadline is None:
                        signal_capture_deadline = (
                            time.monotonic() + _call_chain_capture_grace_seconds()
                        )
                    chain = _check_call_chain_match(post_marker, self.plan)
                    if (
                        chain.get('frame_order_matched')
                        or time.monotonic() >= signal_capture_deadline
                    ):
                        executed_proc.terminate()
                        break
            time.sleep(1)
        if executed_proc.poll() is None:
            executed_proc.kill()
        stdout, stderr = executed_proc.communicate()
        executed = subprocess.CompletedProcess(executed_proc.args, executed_proc.returncode or 0, stdout, stderr)
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


def _check_call_chain_match(log_content: str, plan: TestPlan) -> dict[str, Any]:
    """Match original-log frames in the post-marker serial window only."""
    oracle = plan.call_chain_oracle

    def _canonical_frame(frame: str) -> str:
        """Compare symbols, not build-specific offsets or source annotations."""
        value = str(frame).strip().lstrip("?* ")
        # Kernel Expert may retain a source location beside a symbol, e.g.
        # ``mempool_alloc_noprof mm/mempool.c:402``.  The serial stack only
        # carries the symbol, so keep the raw contract untouched but compare
        # its leading identifier.  This is deliberately lexical/config-driven
        # and does not encode any case-specific function names.
        value = re.split(r"\s+", value, maxsplit=1)[0]
        return re.sub(r"\+0x[0-9a-f]+(?:/0x[0-9a-f]+)?$", "", value, flags=re.IGNORECASE)

    allowed_wrappers = {
        _canonical_frame(wrapper)
        for wrapper in oracle.allowed_wrapper_frames
        if _canonical_frame(wrapper)
    }
    # ``original_call_chain`` is retained as complete first-hand evidence, but
    # only the declared core/top frames are mandatory at runtime. Lower
    # callers and entry/return context can vary with the userspace trigger.
    required_chain = [
        _canonical_frame(frame)
        for frame in (oracle.required_top_frames or oracle.required_frames)
        if _canonical_frame(frame) and _canonical_frame(frame) not in allowed_wrappers
    ]
    result = {
        "required_frames_found": [], "missing_frames": [],
        "frame_order_matched": False,
    }
    marker = f"LUMEN_REPRO_START:{plan.reproduction_case_id}:{plan.target_path_id}"
    lines = log_content.splitlines()
    start = next((index for index, line in enumerate(lines) if marker in line), -1)
    if start < 0:
        alternatives = [
            [str(frame).strip() for frame in group if str(frame).strip()]
            for group in oracle.required_frame_alternatives
        ]
        alternative_members = {frame for group in alternatives for frame in group}
        result["missing_frames"] = [
            frame for frame in required_chain
            if frame not in alternative_members
        ] + [
            group[0] if len(group) == 1 else " or ".join(group)
            for group in alternatives
        ]
        return result
    window = lines[start + 1:]
    def frame_seen(line: str, frame: str) -> bool:
        # Avoid treating ``evict`` as present in the distinct symbol
        # ``jfs_evict_inode``.  Stack symbols are token-like identifiers;
        # boundaries make both presence and ordering deterministic.
        pattern = rf"(?<![A-Za-z0-9_.$]){re.escape(frame)}(?![A-Za-z0-9_.$])"
        return re.search(pattern, line, flags=re.IGNORECASE) is not None

    # ``required_frames`` is retained for backward compatibility, while
    # ``required_frame_alternatives`` describes mutually exclusive branches
    # at one call-chain position (for example session_put OR
    # session_destroy).  Remove alternative members from the singleton
    # groups so a branch is not accidentally treated as two mandatory frames.
    alternative_groups = [
        [_canonical_frame(frame) for frame in group if _canonical_frame(frame)]
        for group in oracle.required_frame_alternatives
    ]
    if required_chain:
        required_set = set(required_chain)
        alternative_groups = [
            group for group in alternative_groups
            if any(frame in required_set for frame in group)
        ]
    alternative_members = {
        frame for group in alternative_groups for frame in group
    }
    required_groups: list[list[str]] = [
        [frame] for frame in required_chain
        if frame not in alternative_members
    ]
    required_groups.extend(alternative_groups)

    order_frames = {
        frame
        for group in required_groups
        for frame in group
    }
    pairs = [
        [_canonical_frame(pair[0]), _canonical_frame(pair[1])]
        for pair in oracle.required_frame_order
        if len(pair) == 2
        and _canonical_frame(pair[0]) in order_frames
        and _canonical_frame(pair[1]) in order_frames
    ]
    # Some historical contracts encoded the accepted stack orientation by
    # declaring both directions for the same frame pair. Treating those
    # edges as simultaneous constraints makes every complete trace fail
    # (A < B and B < A), even though the evaluator below already accepts the
    # whole chain in either leaf-to-caller or caller-to-leaf orientation.
    # Fall back to the authoritative adjacent core chain in that case.
    pair_set = {tuple(pair) for pair in pairs}
    has_bidirectional_edge = any(
        left != right and (right, left) in pair_set
        for left, right in pair_set
    )
    if has_bidirectional_edge and required_chain:
        pairs = [list(pair) for pair in zip(required_chain, required_chain[1:])]
    if required_chain and not pairs:
        # The declared core chain is authoritative for ordering. The complete
        # original log chain remains available in the plan for audit, but its
        # context-sensitive lower frames must not add runtime requirements.
        pairs = [list(pair) for pair in zip(required_chain, required_chain[1:])]

    def _trace_windows() -> list[list[str]]:
        """Split the post-marker log into independent Call Trace blocks."""
        starts = [
            index for index, line in enumerate(window)
            if re.search(r"\bCall Trace:", line, flags=re.IGNORECASE)
        ]
        if not starts:
            # Conservative fallback for logs without a labelled Call Trace.
            return [[
                line for line in window
                if "vcan0:" not in line.lower()
                and not re.search(r"^\s*(?:\[[^\]]+\]\s*)?LUMEN_[A-Z0-9_]+(?:[:=\s]|$)", line, flags=re.IGNORECASE)
                and not re.search(r"\]\s+\?", line)
            ]]

        blocks: list[list[str]] = []
        for trace_start in starts:
            trace_end = len(window)
            for index in range(trace_start + 1, len(window)):
                if re.search(r"</TASK>", window[index], flags=re.IGNORECASE):
                    trace_end = index + 1
                    break
                if re.search(
                    r"\b(?:Allocated by task|Freed by task|The buggy address)",
                    window[index],
                    flags=re.IGNORECASE,
                ):
                    trace_end = index
                    break
                # Oops reports often repeat the RIP/register block immediately
                # after the closing marker.  Keep that duplicate out of the
                # preceding trace so it cannot move the fault leaf after its
                # callers and create a false order mismatch.
                if re.search(r"end\s+trace", window[index], flags=re.IGNORECASE):
                    trace_end = index
                    break
                if re.search(r"\bCall Trace:", window[index], flags=re.IGNORECASE):
                    trace_end = index
                    break
            block = window[trace_start + 1:trace_end]
            # Oops reports commonly put the faulting leaf in the RIP line
            # immediately before ``Call Trace`` and start the trace at its
            # caller. Keep that line attached to this trace block so the
            # authoritative leaf-to-caller chain remains checkable.
            rip_line = None
            for index in range(trace_start - 1, -1, -1):
                candidate = window[index]
                if re.search(
                    r"\bRIP:\s*(?:[0-9a-f]+:)?[A-Za-z_][A-Za-z0-9_.$]*"
                    r"(?:\+0x[0-9a-f]+(?:/0x[0-9a-f]+)?)?",
                    candidate,
                    flags=re.IGNORECASE,
                ):
                    rip_line = candidate
                    break
                if re.search(r"\b(?:Call Trace:|LUMEN_REPRO_START:)", candidate, flags=re.IGNORECASE):
                    break
            if rip_line is not None:
                # The faulting leaf is often printed twice: once in the RIP
                # header and again as the first real stack frame (for
                # example ``RIP: strlen`` followed by ``? skb_put`` and
                # ``? strlen``).  The Call Trace occurrence is authoritative
                # for ordering.  Inserting the duplicate RIP line at index 0
                # would make a caller-to-leaf edge appear reversed and can
                # reject an otherwise complete, exact chain.  Keep RIP only
                # when the trace does not contain that leaf at all.
                rip_match = re.search(
                    r"\bRIP:\s*(?:[0-9a-f]+:)?(?P<frame>[A-Za-z_][A-Za-z0-9_.$]*)",
                    rip_line,
                    flags=re.IGNORECASE,
                )
                rip_frame = rip_match.group("frame") if rip_match else ""
                if not rip_frame or not any(frame_seen(line, rip_frame) for line in block):
                    block.insert(0, rip_line)
            blocks.append(block)
        return blocks

    def _evaluate_trace(trace_lines: list[str]) -> dict[str, Any]:
        """Evaluate one stack without combining evidence from another stack."""
        non_question_lines = [
            line for line in trace_lines
            if not re.search(r"\]\s+\?", line)
        ]
        # A faulting leaf can be printed only as a question-marked frame
        # (for example ? strlen+... after a general-protection exception).
        # Keep that line only when the evidence-backed original chain has no
        # non-question occurrence in this same stack.
        question_fallback_frames = {
            frame for frame in required_chain
            if not any(frame_seen(line, frame) for line in non_question_lines)
        }
        ordering_window = [
            line for line in trace_lines
            if not re.search(r"\]\s+\?", line)
            or any(frame_seen(line, frame) for frame in question_fallback_frames)
        ]
        seen_positions: dict[str, int] = {
            frame: next(
                (index for index, line in enumerate(ordering_window)
                 if frame_seen(line, frame)),
                -1,
            )
            for group in required_groups
            for frame in group
        }
        found_frames: list[str] = []
        missing_frames: list[str] = []
        for group in required_groups:
            found = [frame for frame in group if seen_positions.get(frame, -1) >= 0]
            if found:
                found_frames.extend(found)
            else:
                missing_frames.append(
                    group[0] if len(group) == 1 else " or ".join(group)
                )

        order_positions: dict[str, int] = {
            frame: next(
                (index for index, line in enumerate(ordering_window)
                 if frame_seen(line, frame)),
                -1,
            )
            for frame in order_frames
        }

        def group_position(frame: str) -> int:
            """Return the position of a frame or its satisfied alternative."""
            direct = order_positions.get(frame, -1)
            if direct >= 0:
                return direct
            for group in alternative_groups:
                if frame in group:
                    hits = [order_positions.get(member, -1) for member in group]
                    hits = [position for position in hits if position >= 0]
                    if hits:
                        return min(hits)
            return -1

        forward = all(
            group_position(pair[0]) < group_position(pair[1])
            for pair in pairs
        )
        # Kernel reports commonly print a stack from the faulting leaf toward
        # its callers, while an LLM contract may express the same path from
        # the entry point toward the fault.  Accept either complete orientation,
        # but never accept a partial or scrambled sequence.
        reverse = all(
            group_position(pair[1]) < group_position(pair[0])
            for pair in pairs
        )
        return {
            "required_frames_found": found_frames,
            "missing_frames": missing_frames,
            "frame_order_matched": not missing_frames and (forward or reverse),
            "frame_order_direction": "forward" if forward else ("reverse" if reverse else "mismatch"),
        }

    evaluations = [_evaluate_trace(block) for block in _trace_windows()]
    # Prefer a complete single stack.  If no stack is complete, retain the
    # most informative one for diagnostics; never merge frames across blocks.
    complete = [item for item in evaluations if item["frame_order_matched"]]
    chosen = complete[0] if complete else max(
        evaluations,
        key=lambda item: (len(item["required_frames_found"]), -len(item["missing_frames"])),
    )
    result.update(chosen)
    return result


def _normalise_boot_timeout(value: int | None) -> int:
    """Resolve QemuRecipe.timeout_sec and reject unsafe values."""
    try:
        timeout = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("qemu_recipe.timeout_sec must be an integer") from exc
    if timeout == 0:
        return _DEFAULT_BOOT_TIMEOUT_SEC
    if not 10 <= timeout <= 7200:
        raise ValueError("qemu_recipe.timeout_sec must be 0 or in range 10..7200")
    return timeout


def _normalise_reproducer_timeout(value: int | None) -> int:
    """Resolve the guest C timeout declared by UserspaceReproducer."""
    try:
        timeout = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("reproducer.runtime_timeout_sec must be an integer") from exc
    if timeout == 0:
        return _DEFAULT_REPRODUCER_TIMEOUT_SEC
    if not 1 <= timeout <= _MAX_REPRODUCER_TIMEOUT_SEC:
        raise ValueError(
            "reproducer.runtime_timeout_sec must be 0 or in range 1..7200"
        )
    return timeout


def _ssh_execution_grace_seconds() -> int:
    """Return configurable host-side cleanup grace after guest timeout."""
    raw = os.environ.get(
        "LUMEN_QEMU_SSH_EXECUTION_GRACE_SEC",
        str(_DEFAULT_SSH_EXECUTION_GRACE_SEC),
    )
    try:
        grace = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "LUMEN_QEMU_SSH_EXECUTION_GRACE_SEC must be an integer"
        ) from exc
    if not 0 <= grace <= _MAX_SSH_EXECUTION_GRACE_SEC:
        raise ValueError(
            "LUMEN_QEMU_SSH_EXECUTION_GRACE_SEC must be in range 0..600"
        )
    return grace


def _call_chain_capture_grace_seconds() -> int:
    """Return the post-signal serial-drain window before stopping SSH."""
    raw = os.environ.get(
        "LUMEN_QEMU_CALL_CHAIN_CAPTURE_GRACE_SEC",
        str(_DEFAULT_CALL_CHAIN_CAPTURE_GRACE_SEC),
    )
    try:
        grace = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "LUMEN_QEMU_CALL_CHAIN_CAPTURE_GRACE_SEC must be an integer"
        ) from exc
    if not 0 <= grace <= _MAX_CALL_CHAIN_CAPTURE_GRACE_SEC:
        raise ValueError(
            "LUMEN_QEMU_CALL_CHAIN_CAPTURE_GRACE_SEC must be in range 0..60"
        )
    return grace


def _normalise_concurrent_instances(value: int | None) -> int:
    try:
        instances = int(value or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("qemu_recipe.concurrent_instances must be an integer") from exc
    if not 1 <= instances <= _MAX_CONCURRENT_INSTANCES:
        raise ValueError(
            f"qemu_recipe.concurrent_instances must be in range 1..{_MAX_CONCURRENT_INSTANCES}"
        )
    return instances


def _instance_runtime_root(runtime_root: Path | None, instance: int, count: int) -> Path | None:
    if count == 1:
        return runtime_root
    base = runtime_root if runtime_root is not None else _configured_image_root()
    return base / f"instance-{instance:02d}"


def _run_single_persistent_qemu_test_plan(
    plan: TestPlan, *, attempt: int, runtime_root: Path | None = None,
) -> TestResultContract:
    """Run one isolated userspace-C VM and evaluate its serial call chain."""
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
    # Keep the complete serial log.  The causal and call-chain validators
    # locate LUMEN_REPRO_START themselves, so they remain correct even if
    # QEMU flushes pre-marker bytes after the snapshot used by run_poc().
    content = serial.read_text(encoding="utf-8", errors="replace") if serial.exists() else ""
    matched = _match_serial_signals(log_content=content, detection=plan.detection_signals, expected_signal=plan.expected_signal)
    chain = _check_call_chain_match(content, plan)
    shutdown = manager.shutdown()
    steps.append(shutdown)
    artifacts = {artifact_key: artifact_path for step in steps for artifact_key, artifact_path in step.artifacts.items()}
    causal = _check_causal_reproduction(content, plan, matched) if matched else {}
    consistent = bool(
        matched
        and all(causal.get(field) for field in ("reproducer_started", "signal_after_start", "target_context_matched"))
        and not chain["missing_frames"]
        and chain["frame_order_matched"]
    )
    if consistent:
        return TestResultContract(status="ok", code="PASSED_CALL_CHAIN_CONSISTENT", test_passed=True, attempts=attempt, summary=f"Original call-chain oracle matched after SSH POC start: {matched}", plan=plan, steps=steps, artifacts=artifacts, target_path_id=plan.target_path_id, call_chain_consistent=True, **causal, **chain)
    if matched:
        return TestResultContract(status="failed", code="FAILED_CALL_CHAIN_MISMATCH", attempts=attempt, summary="A target signal was observed but the post-start call chain did not satisfy the original-log oracle.", plan=plan, steps=steps, artifacts=artifacts, target_path_id=plan.target_path_id, **causal, **chain)
    return TestResultContract(status="failed", code="FAILED_SIGNAL_NOT_FOUND", attempts=attempt, summary="No target fault signature was observed after the userspace reproducer started.", plan=plan, steps=steps, artifacts=artifacts, **chain)


def run_persistent_qemu_test_plan(plan: TestPlan, *, attempt: int, runtime_root: Path | None = None) -> TestResultContract:
    """Run one try-out, honoring the declared timeout and VM concurrency."""
    try:
        _validate_execution_steps(plan)
        instances = _normalise_concurrent_instances(plan.qemu_recipe.concurrent_instances)
        _normalise_boot_timeout(plan.qemu_recipe.timeout_sec)
        _normalise_reproducer_timeout(plan.reproducer.runtime_timeout_sec)
        _ssh_execution_grace_seconds()
    except ValueError as exc:
        return TestResultContract(
            status="blocked", code="BLOCKED_EXECUTION_PLAN", attempts=attempt,
            summary=str(exc), plan=plan,
        )
    if instances == 1:
        try:
            return _run_single_persistent_qemu_test_plan(plan, attempt=attempt, runtime_root=runtime_root)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return TestResultContract(status="blocked", code="BLOCKED_PERSISTENT_QEMU", attempts=attempt, summary=str(exc), plan=plan)

    results: list[tuple[int, TestResultContract]] = []
    with ThreadPoolExecutor(max_workers=instances, thread_name_prefix="lumen-qemu") as executor:
        futures = {
            executor.submit(
                _run_single_persistent_qemu_test_plan,
                plan,
                attempt=attempt,
                runtime_root=_instance_runtime_root(runtime_root, index, instances),
            ): index
            for index in range(1, instances + 1)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results.append((index, future.result()))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                results.append((index, TestResultContract(status="blocked", code="BLOCKED_PERSISTENT_QEMU", attempts=attempt, summary=str(exc), plan=plan)))
    results.sort(key=lambda item: item[0])
    winner = next((result for _, result in results if result.test_passed), None)
    if winner is None:
        # Preserve the most informative failure (a signal/call-chain mismatch
        # is more useful than a VM setup failure), while retaining every
        # instance artifact for diagnosis.
        winner = max((result for _, result in results), key=lambda item: (bool(item.required_frames_found), bool(item.signal_after_start), item.status == "failed"))
    winner.steps = [step for _, result in results for step in result.steps]
    winner.artifacts = {
        f"instance_{index:02d}_{key}": value
        for index, result in results
        for key, value in result.artifacts.items()
    }
    passed = [index for index, result in results if result.test_passed]
    winner.summary = f"{len(passed)}/{instances} concurrent QEMU instances passed call-chain verification." if passed else f"{instances} concurrent QEMU instances completed without a consistent call-chain reproduction."
    return winner
