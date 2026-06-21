"""QEMU testing tools for LangChain/LangGraph tool calling.

Provides LangChain StructuredTool wrappers for QEMU kernel testing,
enabling test_expert to execute real QEMU verification in real execution mode.

Uses scripts from Analysis-SKILL/skills/qemu-test/scripts/.
"""

import subprocess
import tempfile
import os
from pathlib import Path
from typing import Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from agents.contracts import ToolStepResult
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
    timeout: int = 120
    memory: str = "512M"


class AnalyzeLogInput(BaseModel):
    """Input schema for analyze_boot_log."""
    log_path: str
    patterns: Optional[list[str]] = None


def _resolve_runtime_path(path: str | Path) -> Path:
    """Resolve user/model-provided paths before passing them to skill scripts."""
    p = Path(os.path.expanduser(str(path)))
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


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
    output_path: Optional[str] = None,
) -> str:
    """Create minimal initramfs for QEMU kernel testing.

    Args:
        arch: Target architecture
        test_script_path: Optional test script to include
        modules_dir: Optional directory containing kernel modules to include
        output_path: Optional output path for initramfs

    Returns:
        Path to created initramfs or error message
    """
    arch = _normalize_arch(arch)
    script_path = find_qemu_script("create_initramfs.sh")

    if not script_path:
        return "Error: create_initramfs.sh not found in qemu-test skill"

    if output_path is None:
        output_path = str(tempfile.mktemp(suffix=".cpio.gz", prefix="initramfs_"))
    else:
        output_path = str(_resolve_runtime_path(output_path))

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
            size = Path(output_path).stat().st_size
            return f"✓ Initramfs created\n  Path: {output_path}\n  Size: {size // 1024} KB\n  Arch: {arch}"
        else:
            return f"Error: initramfs not created at {output_path}"

    except subprocess.TimeoutExpired:
        return "Error: initramfs creation timed out (60s)"
    except Exception as e:
        return f"Error: {str(e)}"


def boot_kernel(
    kernel_path: str,
    initramfs_path: str,
    arch: str = "x86_64",
    timeout: int = 120,
    memory: str = "512M",
) -> str:
    """Boot kernel in QEMU and capture boot log.

    Args:
        kernel_path: Path to kernel image (vmlinux or Image)
        initramfs_path: Path to initramfs/initrd
        arch: Target architecture
        timeout: Boot timeout in seconds
        memory: Memory allocation

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

    # Find boot script
    boot_script_map = {
        "x86_64": "boot_x86.sh",
        "arm64": "boot_arm64.sh",
        "arm32": "boot_arm32.sh",
    }
    boot_script_name = boot_script_map.get(arch, "boot_x86.sh")
    boot_script_path = find_qemu_script(boot_script_name)

    if not boot_script_path:
        return f"Error: boot script not found: {boot_script_name}"

    # Create temp log file
    log_path = tempfile.mktemp(suffix=".log", prefix="qemu_boot_")

    try:
        cmd = [
            "bash", str(boot_script_path),
            "--kernel", str(kernel),
            "--initrd", str(initramfs),
            "--timeout", str(timeout),
            "--memory", memory,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 30,  # Extra buffer for script overhead
            cwd=str(boot_script_path.parent),
        )

        # Combine stdout and stderr as boot log
        boot_log = result.stdout + "\n" + result.stderr

        # Save log
        Path(log_path).write_text(boot_log)

        # Analyze boot result
        exit_status = result.returncode

        if exit_status == 0:
            status = "✓ Boot completed successfully"
        elif exit_status == 124 or "Timeout" in boot_log:
            status = "⚠ Boot timed out"
        else:
            status = f"✗ Boot failed (exit: {exit_status})"

        # Extract key info from log
        kernel_version = ""
        if "Linux version" in boot_log:
            for line in boot_log.split("\n"):
                if "Linux version" in line:
                    kernel_version = line.strip()
                    break

        panic_detected = "Kernel panic" in boot_log or "BUG:" in boot_log
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

    # Default patterns for kernel errors
    default_patterns = [
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

    search_patterns = patterns or default_patterns

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
    output_path: Optional[str] = None,
) -> ToolStepResult:
    """Structured wrapper around create_initramfs."""
    normalized_arch = _normalize_arch(arch)
    output = create_initramfs(
        arch=normalized_arch,
        test_script_path=test_script_path,
        modules_dir=modules_dir,
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
    timeout: int = 120,
    memory: str = "512M",
) -> ToolStepResult:
    """Structured wrapper around boot_kernel."""
    normalized_arch = _normalize_arch(arch)
    output = boot_kernel(
        kernel_path=kernel_path,
        initramfs_path=initramfs_path,
        arch=normalized_arch,
        timeout=timeout,
        memory=memory,
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
