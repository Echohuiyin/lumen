"""Standalone test for QEMU testing tools used by test_expert.

Exercises each QEMU tool independently to verify they work
before relying on them in the full LangGraph workflow.

Usage:
    python3 tests/test_qemu_tools.py [--kernel /path/to/vmlinux]

If --kernel is not provided, only check_qemu_available is tested.
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from agents.qemu_tools import (
    check_qemu_available,
    create_initramfs,
    boot_kernel,
    analyze_boot_log,
    create_qemu_tools,
)


def test_header(name: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  TEST: {name}")
    print(f"{'=' * 60}")


def test_check_qemu() -> bool:
    """Test QEMU availability check."""
    test_header("check_qemu_available")
    for arch in ["x86_64", "arm64"]:
        result = check_qemu_available(arch)
        print(f"  [{arch}] {result[:200]}")
    return True


def test_create_initramfs() -> bool:
    """Test initramfs creation."""
    test_header("create_initramfs")

    result = create_initramfs(arch="x86_64")
    print(f"  Result: {result[:200]}")

    if "Error" in result:
        print("  WARN: initramfs creation failed (may be ok if scripts not found)")
        return False
    return True


def test_boot_kernel(kernel_path: str | None) -> bool:
    """Test kernel booting in QEMU."""
    if not kernel_path:
        print("  SKIP: No kernel path provided (use --kernel)")
        return True

    test_header("boot_kernel")

    expanded = os.path.expanduser(kernel_path)
    if not Path(expanded).exists():
        print(f"  SKIP: Kernel not found: {expanded}")
        return True

    # Create initramfs first
    initramfs_result = create_initramfs(arch="x86_64")
    if "Error" in initramfs_result:
        print(f"  SKIP: Cannot create initramfs: {initramfs_result[:100]}")
        return True

    # Extract initramfs path from result
    initramfs_path = None
    for line in initramfs_result.split("\n"):
        if "Path:" in line:
            initramfs_path = line.split("Path:")[-1].strip()
            break

    if not initramfs_path or not Path(initramfs_path).exists():
        print(f"  SKIP: Initramfs path not found in: {initramfs_result[:200]}")
        return True

    print(f"  Booting kernel: {expanded}")
    print(f"  Initramfs: {initramfs_path}")

    result = boot_kernel(
        kernel_path=expanded,
        initramfs_path=initramfs_path,
        arch="x86_64",
        timeout=30,
        memory="256M",
    )
    print(f"  Result: {result[:500]}")
    return True


def test_analyze_boot_log(log_path: str | None) -> bool:
    """Test boot log analysis."""
    test_header("analyze_boot_log")

    if log_path:
        expanded = os.path.expanduser(log_path)
    else:
        # Try to find any existing boot log
        candidates = [
            "/tmp/qemu_boot_*.log",
            "/tmp/lumen_outputs/*.log",
        ]
        import glob
        expanded = None
        for pattern in candidates:
            matches = glob.glob(os.path.expanduser(pattern))
            if matches:
                expanded = matches[0]
                break

    if not expanded or not Path(expanded).exists():
        # Create a minimal fake log for testing
        fd, expanded = tempfile.mkstemp(suffix=".log", prefix="test_boot_")
        with os.fdopen(fd, "w") as f:
            f.write("Linux version 5.15.0\n")
            f.write("Kernel panic - not syncing: hung_task: blocked tasks\n")
            f.write("Call Trace:\n")
            f.write(" __schedule+0x123/0x456\n")
        print(f"  Created test log: {expanded}")

    result = analyze_boot_log(log_path=expanded, patterns=None)
    print(f"  Result: {result[:300]}")
    return True


def test_langchain_tool_creation() -> bool:
    """Test that LangChain StructuredTools can be created."""
    test_header("create_qemu_tools (LangChain binding)")

    tools = create_qemu_tools()
    print(f"  Created {len(tools)} tools:")

    all_valid = True
    for tool in tools:
        has_name = bool(tool.name)
        has_desc = bool(tool.description)
        has_func = callable(tool.func)
        has_schema = tool.args_schema is not None
        status = "OK" if all([has_name, has_desc, has_func, has_schema]) else "MISSING"
        if status != "OK":
            all_valid = False
        print(f"    {tool.name}: name={has_name} desc={has_desc} "
              f"func={has_func} schema={has_schema} [{status}]")

    return all_valid


def main():
    parser = argparse.ArgumentParser(description="Test QEMU testing tools")
    parser.add_argument("--kernel", help="Path to vmlinux/Image for boot test")
    parser.add_argument("--log", help="Path to boot log for analysis test")
    args = parser.parse_args()

    results = {}

    # Test 1: QEMU check (no dependencies)
    results["check_qemu"] = test_check_qemu()

    # Test 2: LangChain tool creation (no dependencies)
    results["tool_creation"] = test_langchain_tool_creation()

    # Test 3: Initramfs creation
    results["create_initramfs"] = test_create_initramfs()

    # Test 4: Boot log analysis
    results["analyze_boot_log"] = test_analyze_boot_log(args.log)

    # Test 5: Kernel boot (needs kernel path)
    results["boot_kernel"] = test_boot_kernel(args.kernel)

    # Summary
    print(f"\n{'=' * 60}")
    print("  SUMMARY")
    print(f"{'=' * 60}")
    all_pass = True
    for name, passed in results.items():
        status = "PASS" if passed else "WARN"
        if not passed:
            all_pass = False
        print(f"  {name}: {status}")

    if all_pass:
        print("\n  All tests passed!")
    else:
        print("\n  Some tests had warnings (may be expected in this environment)")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
