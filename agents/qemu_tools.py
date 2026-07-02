"""QEMU testing tools for LangChain/LangGraph tool calling.

Provides LangChain StructuredTool wrappers for QEMU kernel testing,
enabling test_expert to execute real QEMU verification in real execution mode.

Uses scripts from Analysis-SKILL/skills/qemu-test/scripts/.
"""

import re
import shutil
import subprocess
import tempfile
import os
from pathlib import Path
from typing import Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from agents.contracts import QemuRecipe, ToolStepResult
from paths import PROJECT_ROOT, get_skill_path_candidates


class CheckQemuInput(BaseModel):
    """Input schema for check_qemu_available."""
    arch: str = "x86_64"


class CreateInitramfsInput(BaseModel):
    """Input schema for create_initramfs."""
    arch: str = "x86_64"
    test_script_path: Optional[str] = None
    modules_dir: Optional[str] = None
    output_path: Optional[str] = None


class BootKernelInput(BaseModel):
    """Input schema for boot_kernel."""
    kernel_path: str
    initramfs_path: str
    arch: str = "x86_64"
    timeout: int = 300
    memory: str = ""


class AnalyzeLogInput(BaseModel):
    """Input schema for analyze_boot_log."""
    log_path: str
    patterns: Optional[list[str]] = None


# Kernel error patterns scanned by default. Boot-time KASAN panics
# (e.g. "kasan_populate_shadow: Failed to allocate page") surface as a
# "Kernel panic" line, so keeping that token here is load-bearing for
# detecting OOM-style boot failures that would otherwise be misclassified
# as plain timeouts.
_DEFAULT_BOOT_ERROR_PATTERNS: list[str] = [
    "Kernel panic",
    "BUG:",
    "Oops:",
    "NULL pointer",
    "soft lockup",
    "blocked for more than",
    "hung task",
    "stack-overflow",
    "Call Trace:",
]


def _resolve_runtime_path(path: str | Path) -> Path:
    """Resolve user/model-provided paths before passing them to skill scripts."""
    p = Path(os.path.expanduser(str(path)))
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


def _detect_kernel_type(kernel_path: str) -> str:
    """Return elf, bzimage, raw_image, or unknown."""
    try:
        with open(kernel_path, "rb") as f:
            header = f.read(4)
        if header == b"\x7fELF":
            return "elf"
        if header[:2] == b"MZ" or header == b"HdrS":
            return "bzimage"
        return "raw_image"
    except Exception:
        return "unknown"


def _select_qemu_memory(kernel_path: str, requested: str = "") -> str:
    """Pick a QEMU memory size that fits the kernel.

    KASAN-enabled kernels need a large shadow region at boot, so a 512MB QEMU
    guest panics during `kasan_populate_shadow` before any test code runs.
    Heuristic: bzImage files >= 20MB almost always come from a KASAN/debug
    build (syzbot kernels land around 100MB). Such kernels get 2GB; smaller
    kernels keep the legacy 512MB. An explicit `requested` value overrides
    the heuristic so kernel_expert / contracts can pin a specific size.
    """
    if requested:
        return requested
    try:
        size = os.path.getsize(kernel_path)
    except OSError:
        return "512M"
    return "2G" if size >= 20 * 1024 * 1024 else "512M"


def _normalize_arch(arch: str | None) -> str:
    value = (arch or "x86_64").lower()
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


def find_qemu_script(script_name: str) -> Optional[Path]:
    """Find QEMU test script in skill directories.

    Args:
        script_name: Name of script (e.g., 'boot_x86.sh', 'create_initramfs.sh')

    Returns:
        Path to script or None if not found
    """
    skill_paths = get_skill_path_candidates("qemu-test")

    for skill_path in skill_paths:
        script_path = skill_path / "scripts" / script_name
        if script_path.exists():
            return script_path

    return None


def check_qemu_available(arch: str = "x86_64") -> str:
    """Check if QEMU binary is available for the specified architecture.

    Args:
        arch: Architecture to check (x86_64, arm64, arm32)

    Returns:
        Status message with QEMU availability info
    """
    arch = _normalize_arch(arch)
    qemu_map = {
        "x86_64": "qemu-system-x86_64",
        "arm64": "qemu-system-aarch64",
        "arm32": "qemu-system-arm",
    }

    qemu_binary = qemu_map.get(arch, "qemu-system-x86_64")

    try:
        result = subprocess.run(
            ["which", qemu_binary],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode == 0:
            path = result.stdout.strip()
            # Get version info
            version_result = subprocess.run(
                [qemu_binary, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            version = version_result.stdout.split("\n")[0] if version_result.returncode == 0 else "unknown"

            return f"✓ QEMU available for {arch}\n  Binary: {path}\n  Version: {version}"
        else:
            return f"✗ QEMU not found for {arch}\n  Required: {qemu_binary}\n  Install: apt install qemu-system-{arch.replace('arm64', 'arm').replace('arm32', 'arm')}"
    except Exception as e:
        return f"Error checking QEMU: {str(e)}"


def create_initramfs(
    arch: str = "x86_64",
    test_script_path: Optional[str] = None,
    modules_dir: Optional[str] = None,
    binaries_dir: Optional[str] = None,
    output_path: Optional[str] = None,
) -> str:
    """Create minimal initramfs for QEMU kernel testing.

    Args:
        arch: Target architecture
        test_script_path: Optional test script to include
        modules_dir: Optional directory containing kernel modules to include
        binaries_dir: Optional directory containing userspace binaries (e.g.
            trigger programs) to include in /bin inside the initramfs
        output_path: Optional output path for initramfs

    Returns:
        Path to created initramfs or error message

    Caches the cpio.gz on disk keyed by (arch, script_mtime, test_script,
    modules, binaries). Test retries with unchanged inputs hit the cache
    and skip the 10-30s cpio rebuild. See agents/cache/initramfs_cache.py.
    """
    arch = _normalize_arch(arch)
    script_path = find_qemu_script("create_initramfs.sh")

    if not script_path:
        return "Error: create_initramfs.sh not found in qemu-test skill"

    if output_path is None:
        output_path = str(tempfile.mktemp(suffix=".cpio.gz", prefix="initramfs_"))
    else:
        output_path = str(_resolve_runtime_path(output_path))

    # ---- Cache C: short-circuit if identical inputs already produced a cpio.gz
    from agents.cache.initramfs_cache import get_or_build_key, lookup, store
    cache_key = get_or_build_key(
        arch=arch,
        script_path=Path(script_path),
        test_script_path=test_script_path,
        modules_dir=modules_dir,
        binaries_dir=binaries_dir,
    )
    cached = lookup(cache_key)
    if cached:
        import shutil
        try:
            shutil.copy2(cached, output_path)
            size = Path(output_path).stat().st_size
            return f"✓ Initramfs created (cached)\n  Path: {output_path}\n  Size: {size // 1024} KB\n  Arch: {arch}"
        except OSError as e:
            # Cache copy failed — fall through to rebuild
            pass
    # ---- end cache lookup

    cmd = ["bash", str(script_path), "--arch", arch, "--output", output_path]

    if test_script_path:
        test_script = _resolve_runtime_path(test_script_path)
        if not test_script.exists():
            return f"Error: test script not found: {test_script_path}"
        cmd.extend(["--test-script", str(test_script)])

    if modules_dir:
        module_path = _resolve_runtime_path(modules_dir)
        if module_path.is_file() and module_path.suffix == ".ko":
            module_path = module_path.parent
        if not module_path.exists() or not module_path.is_dir():
            return f"Error: modules dir not found: {modules_dir}"
        cmd.extend(["--modules", str(module_path)])

    if binaries_dir:
        binaries_path = _resolve_runtime_path(binaries_dir)
        if not binaries_path.exists() or not binaries_path.is_dir():
            return f"Error: binaries dir not found: {binaries_dir}"
        cmd.extend(["--binaries", str(binaries_path)])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(script_path.parent),
        )

        if result.returncode != 0:
            return f"Error creating initramfs: {result.stderr[:500]}"

        if Path(output_path).exists():
            # ---- Cache C: store the freshly built cpio.gz for future hits
            store(cache_key, output_path)
            # ---- end store
            size = Path(output_path).stat().st_size
            return f"✓ Initramfs created\n  Path: {output_path}\n  Size: {size // 1024} KB\n  Arch: {arch}"
        else:
            return f"Error: initramfs not created at {output_path}"

    except subprocess.TimeoutExpired:
        return "Error: initramfs creation timed out (60s)"
    except Exception as e:
        return f"Error: {str(e)}"


# ---------------------------------------------------------------------------
# NUMA topology auto-detection
# ---------------------------------------------------------------------------

def _detect_numa_fake(bzimage_path: str) -> int:
    """Extract numa_fake=N from bzImage CONFIG_CMDLINE.

    Uses extract-ikconfig to read the syzbot kernel's built-in config and
    parse the numa_fake= parameter.  Returns N (>=2) if found, 0 if the
    kernel doesn't have IKCONFIG embedded or doesn't use numa_fake.

    Cached on disk by bzImage fingerprint — see agents/cache/ikconfig_cache.py.

    This lets boot_kernel() auto-configure matching QEMU NUMA topology so
    that set_cpu_sibling_map doesn't WARN on fake NUMA partition, without
    needing per-case configuration or disabling NUMA entirely.
    """
    from agents.cache.ikconfig_cache import get_config_cmdline
    cmdline = get_config_cmdline(bzimage_path)
    if not cmdline:
        return 0
    m = re.search(r'numa=fake=(\d+)', cmdline)
    return int(m.group(1)) if m else 0


def _parse_qemu_memory_mb(memory: str) -> int:
    """Parse QEMU -m string (e.g. '2G', '1024M') to MiB.

    Returns 512 (the default) when the string can't be parsed.
    """
    m = re.match(r"(\d+)\s*([MG])", memory.upper())
    if not m:
        return 512
    val = int(m.group(1))
    unit = m.group(2)
    return val * 1024 if unit == "G" else val


def _build_numa_qemu_args(n: int, smp_spec: str, memory: str) -> list[str]:
    """Build QEMU -numa arguments for N NUMA nodes.

    Distributes CPUs evenly across nodes so the physical topology matches
    what numa_fake=N would create — avoiding set_cpu_sibling_map WARNING
    while preserving NUMA coverage for downstream test cases.

    Also allocates memory per node via ``-object memory-backend-ram`` and
    ``-numa node,memdev=...`` so QEMU >= 8.2 (Debian) doesn't reject the
    config with "total memory for NUMA nodes (0x0) should equal RAM size".

    Returns an empty list when N <= 1 (no NUMA args needed).
    """
    if n <= 1:
        return []
    try:
        smp = int(smp_spec)
    except ValueError:
        m = re.search(r"(\d+)", smp_spec)
        if not m:
            return []
        smp = int(m.group(1))
    if smp <= 1:
        return []

    total_mb = _parse_qemu_memory_mb(memory)
    per_node_mb = total_mb // n

    cpus_per = max(1, smp // n)
    args: list[str] = []
    for i in range(n):
        start = i * cpus_per
        end = start + cpus_per - 1
        if start >= smp:
            break
        if end >= smp:
            end = smp - 1
        args.extend([
            "-object", f"memory-backend-ram,id=mem{i},size={per_node_mb}M",
            "-numa", f"node,nodeid={i},memdev=mem{i},cpus={start}-{end}",
        ])
    return args


def boot_kernel(
    kernel_path: str,
    initramfs_path: str,
    arch: str = "x86_64",
    timeout: int = 300,
    memory: str = "",
    qemu_recipe: "QemuRecipe | None" = None,
) -> str:
    """Boot kernel in QEMU and capture boot log.

    Writes QEMU serial output directly to a file via -serial file: option,
    bypassing any shell pipe buffering issues that could cause empty logs.

    Args:
        kernel_path: Path to kernel image (vmlinux or Image)
        initramfs_path: Path to initramfs/initrd
        arch: Target architecture
        timeout: Boot timeout in seconds
        memory: Memory allocation; empty = auto-select based on kernel size
        qemu_recipe: Optional structured QEMU config from kernel_expert contract.
            When provided, overrides the legacy hardcoded smp_spec="2" / i440FX /
            memory auto-select. Empty fields fall back to defaults.

    Returns:
        Boot result with log content or error message
    """
    arch = _normalize_arch(arch)

    # Validate inputs
    kernel = _resolve_runtime_path(kernel_path)
    initramfs = _resolve_runtime_path(initramfs_path)

    if not kernel.exists():
        return f"Error: kernel not found: {kernel_path}"
    if not initramfs.exists():
        return f"Error: initramfs not found: {initramfs_path}"

    # Auto-select memory: KASAN/debug kernels need >=2GB or they panic during
    # kasan_populate_shadow before any test code runs.
    memory = _select_qemu_memory(str(kernel), memory)

    # Create log file for serial output (written directly by QEMU)
    serial_log = tempfile.mktemp(suffix=".log", prefix="qemu_serial_")

    kernel_type = _detect_kernel_type(str(kernel))
    if kernel_type == "elf":
        return (
            f"✗ Kernel is ELF vmlinux (debug symbols only, not bootable)\n"
            f"  Kernel: {kernel}\n"
            f"  QEMU requires a bzImage for {arch}."
        )

    try:
        # Build QEMU command directly — no intermediate shell script,
        # no pipe buffering. Serial output goes directly to a file via
        # -serial file: which is the most reliable capture method.
        # kasan.fault=panic: KASAN reports (UAF/OOB) panic the kernel — required
        #   for KASAN fault reproducers to leave vmcore evidence.
        # oops=panic: kernel oops (NULL deref, BUG_ON) escalates to panic.
        # hung_task_panic=1 + hung_task_timeout_secs=60: khungtaskd panics on
        #   D-state tasks blocked >= 60s — required for deadlock reproducers.
        # These flags are inert for kernels/bug types that don't trigger them.
        cmdline = (
            "console=ttyS0 root=/dev/ram rw panic=1 "
            "oops=panic kasan.fault=panic "
            "hung_task_panic=1 hung_task_timeout_secs=60"
        )

        # Apply QemuRecipe overrides from kernel_expert contract.
        # Empty fields fall back to legacy defaults so an empty recipe() is
        # equivalent to calling boot_kernel without a recipe.
        recipe = qemu_recipe
        machine_spec = (recipe.machine if recipe and recipe.machine else "accel=kvm:tcg")
        cpu_spec = (recipe.cpu if recipe and recipe.cpu else "host")
        smp_spec = (recipe.smp if recipe and recipe.smp else "2")
        if recipe and recipe.extra_cmdline:
            cmdline = cmdline + " " + recipe.extra_cmdline

        # Auto-detect numa_fake=N from syzbot kernel config and generate
        # matching QEMU NUMA topology.  Syzbot kernels commonly embed
        # numa_fake=N + panic_on_warn=1 in CONFIG_CMDLINE; without real
        # NUMA topology, set_cpu_sibling_map WARNINGs on the fake split,
        # which panic_on_warn=1 escalates to a boot-time panic.
        # Using real NUMA nodes avoids the WARNING while preserving NUMA
        # coverage for downstream test cases.
        numa_fake_n = _detect_numa_fake(str(kernel))
        numa_args = _build_numa_qemu_args(numa_fake_n, smp_spec, memory)

        qemu_cmd = [
            "qemu-system-x86_64",
            "-machine", machine_spec,
            "-cpu", cpu_spec,
            "-smp", smp_spec,
            "-m", memory,
            "-display", "none",
            "-serial", f"file:{serial_log}",
            "-no-reboot",
            "-kernel", str(kernel),
            "-initrd", str(initramfs),
            "-append", cmdline,
        ]
        # Inject auto-detected NUMA topology after -smp so the node
        # config is available when QEMU parses the CPU topology.
        if numa_args:
            ins = qemu_cmd.index("-smp") + 2
            qemu_cmd[ins:ins] = numa_args

        result = subprocess.run(
            qemu_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        # Read serial log (primary output source)
        boot_log = ""
        serial_path = Path(serial_log)
        if serial_path.exists():
            boot_log = serial_path.read_text(encoding="utf-8", errors="replace")

        # Fallback: if serial log is empty, use subprocess captured output
        if not boot_log.strip():
            boot_log = result.stdout + "\n" + result.stderr

        # Also save to a known path for test_runner
        log_path = serial_log

        # Analyze boot result
        exit_status = result.returncode
        # timeout(1) exits 124; QEMU with -no-reboot exits on panic;
        # normal poweroff exits 0
        has_panic = "Kernel panic" in boot_log or "BUG:" in boot_log
        has_hung = "hung_task" in boot_log or "blocked for more than" in boot_log

        if exit_status == 0:
            status = "✓ Boot completed successfully (poweroff)"
        elif has_panic or has_hung:
            status = "✓ Boot completed with expected kernel panic"
        elif exit_status == 124 or exit_status == 143:
            status = "⚠ Boot timed out"
        else:
            status = f"✗ Boot failed (exit: {exit_status})"

        # Extract kernel version and panic info
        kernel_version = ""
        for line in boot_log.split("\n"):
            if "Linux version" in line:
                kernel_version = line.strip()
                break

        panic_detected = has_panic or has_hung
        boot_log_lines = boot_log.splitlines()
        last_lines = "\n".join(boot_log_lines[-20:]) if len(boot_log_lines) > 20 else boot_log

        return f"""{status}
  Kernel: {kernel}
  Initramfs: {initramfs}
  Arch: {arch}
  Memory: {memory}
  Timeout: {timeout}s

Boot Log saved to: {log_path}
Log size: {len(boot_log)} bytes

Kernel Version: {kernel_version}

Panic Detected: {panic_detected}

Last 20 lines:
{last_lines}
"""

    except subprocess.TimeoutExpired:
        # On timeout, still check if serial log has partial output
        partial = ""
        serial_path = Path(serial_log)
        if serial_path.exists():
            partial = serial_path.read_text(encoding="utf-8", errors="replace")
        if partial.strip():
            log_path = serial_log
            Path(log_path).write_text(partial)
            return f"""⚠ Boot timed out ({timeout}s) — partial output captured
  Kernel: {kernel}
  Initramfs: {initramfs}

Boot Log saved to: {log_path}
Log size: {len(partial)} bytes

Last 20 lines:
{chr(10).join(partial.splitlines()[-20:])}
"""
        return f"Error: QEMU boot timed out ({timeout}s)"
    except Exception as e:
        return f"Error booting kernel: {str(e)}"


def analyze_boot_log(
    log_path: str,
    patterns: Optional[list[str]] = None,
) -> str:
    """Analyze QEMU boot log for errors and patterns.

    Args:
        log_path: Path to boot log file
        patterns: Optional list of patterns to search

    Returns:
        Analysis summary
    """
    log_file = _resolve_runtime_path(log_path)
    if not log_file.exists():
        return f"Error: log file not found: {log_path}"

    try:
        log_content = log_file.read_text()
    except Exception as e:
        return f"Error reading log: {str(e)}"

    search_patterns = patterns or _DEFAULT_BOOT_ERROR_PATTERNS

    findings = []
    for pattern in search_patterns:
        matches = []
        for line in log_content.split("\n"):
            if pattern.lower() in line.lower():
                matches.append(line.strip())

        if matches:
            findings.append(f"\n### {pattern}\n{matches[0]}")
            if len(matches) > 1:
                findings.append(f"  ... and {len(matches) - 1} more matches")

    if findings:
        total_lines = len(log_content.splitlines())
        return f"""Boot Log Analysis
Log: {log_file}
Size: {len(log_content)} bytes

Key Findings:
{''.join(findings)}

Summary:
- Total lines: {total_lines}
- Error patterns found: {len(findings)}
"""
    else:
        return f"""Boot Log Analysis
Log: {log_file}
Size: {len(log_content)} bytes

No error patterns detected.
Boot appears successful.
"""


def _extract_labeled_value(text: str, label: str) -> str:
    """Extract a simple `Label: value` field from tool text output."""
    prefix = f"{label}:"
    for line in text.splitlines():
        if line.strip().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def check_qemu_available_result(arch: str = "x86_64") -> ToolStepResult:
    """Structured wrapper around check_qemu_available."""
    normalized_arch = _normalize_arch(arch)
    output = check_qemu_available(normalized_arch)
    status = "ok" if "QEMU available" in output else "skipped"
    return ToolStepResult(
        name="check_qemu_available",
        status=status,
        message="QEMU available" if status == "ok" else "QEMU missing",
        inputs={"arch": normalized_arch},
        output=output,
        error="" if status == "ok" else output,
    )


def create_initramfs_result(
    arch: str = "x86_64",
    test_script_path: Optional[str] = None,
    modules_dir: Optional[str] = None,
    binaries_dir: Optional[str] = None,
    output_path: Optional[str] = None,
) -> ToolStepResult:
    """Structured wrapper around create_initramfs."""
    normalized_arch = _normalize_arch(arch)
    output = create_initramfs(
        arch=normalized_arch,
        test_script_path=test_script_path,
        modules_dir=modules_dir,
        binaries_dir=binaries_dir,
        output_path=output_path,
    )
    initramfs_path = _extract_labeled_value(output, "Path")
    ok = output.startswith("✓ Initramfs created") and bool(initramfs_path)
    artifacts = {"initramfs_path": initramfs_path} if initramfs_path else {}
    return ToolStepResult(
        name="create_initramfs",
        status="ok" if ok else "failed",
        message="initramfs created" if ok else "initramfs creation failed",
        inputs={
            "arch": normalized_arch,
            "test_script_path": test_script_path or "",
            "modules_dir": modules_dir or "",
            "binaries_dir": binaries_dir or "",
            "output_path": output_path or "",
        },
        artifacts=artifacts,
        output=output,
        error="" if ok else output,
    )


def boot_kernel_result(
    kernel_path: str,
    initramfs_path: str,
    arch: str = "x86_64",
    timeout: int = 300,
    memory: str = "",
    qemu_recipe: Optional[QemuRecipe] = None,
) -> ToolStepResult:
    """Structured wrapper around boot_kernel."""
    normalized_arch = _normalize_arch(arch)
    output = boot_kernel(
        kernel_path=kernel_path,
        initramfs_path=initramfs_path,
        arch=normalized_arch,
        timeout=timeout,
        memory=memory,
        qemu_recipe=qemu_recipe,
    )
    log_path = _extract_labeled_value(output, "Boot Log saved to")
    artifacts = {"boot_log_path": log_path} if log_path else {}
    if output.startswith("✓ Boot completed successfully"):
        status = "ok"
        message = "boot completed"
    elif "timed out" in output.lower() or output.startswith("⚠ Boot timed out"):
        status = "failed"
        message = "boot timed out"
    else:
        status = "failed"
        message = "boot failed"
    return ToolStepResult(
        name="boot_kernel",
        status=status,
        message=message,
        inputs={
            "kernel_path": kernel_path,
            "initramfs_path": initramfs_path,
            "arch": normalized_arch,
            "timeout": timeout,
            "memory": memory,
        },
        artifacts=artifacts,
        output=output,
        error="" if status == "ok" else output,
    )


def analyze_boot_log_result(
    log_path: str,
    patterns: Optional[list[str]] = None,
) -> ToolStepResult:
    """Structured wrapper around analyze_boot_log."""
    output = analyze_boot_log(log_path=log_path, patterns=patterns)
    ok = not output.startswith("Error:")
    findings = "Error patterns found:" in output and "Error patterns found: 0" not in output
    return ToolStepResult(
        name="analyze_boot_log",
        status="ok" if ok else "failed",
        message="patterns found" if findings else "no patterns found",
        inputs={"log_path": log_path, "patterns": patterns or []},
        artifacts={"boot_log_path": log_path},
        output=output,
        error="" if ok else output,
    )


def create_qemu_tools() -> list[StructuredTool]:
    """Create LangChain StructuredTool instances for QEMU testing.

    Returns:
        List of StructuredTool instances for bind_tools()
    """
    tools = [
        StructuredTool(
            name="check_qemu_available",
            description=(
                "Check if QEMU is installed and available for the specified architecture. "
                "Use before attempting to boot kernels in QEMU. "
                "Returns QEMU path and version info."
            ),
            func=check_qemu_available,
            args_schema=CheckQemuInput,
        ),
        StructuredTool(
            name="create_initramfs",
            description=(
                "Create minimal initramfs for QEMU kernel testing. "
                "Includes busybox and essential init scripts. "
                "Optionally includes a test script and kernel modules for automated testing. "
                "Returns path to created initramfs."
            ),
            func=create_initramfs,
            args_schema=CreateInitramfsInput,
        ),
        StructuredTool(
            name="boot_kernel",
            description=(
                "Boot a kernel in QEMU with specified initramfs. "
                "Captures boot log and detects kernel panics or errors. "
                "Returns boot status and log analysis. "
                "Use for verifying kernel functionality or reproducing issues."
            ),
            func=boot_kernel,
            args_schema=BootKernelInput,
        ),
        StructuredTool(
            name="analyze_boot_log",
            description=(
                "Analyze QEMU boot log for kernel errors, panics, and patterns. "
                "Searches for common kernel error patterns like panic, Oops, soft lockup. "
                "Returns summary of findings."
            ),
            func=analyze_boot_log,
            args_schema=AnalyzeLogInput,
        ),
    ]

    return tools
