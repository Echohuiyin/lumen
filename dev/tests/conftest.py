"""Pytest fixtures for tests originally designed as standalone scripts.

These tests use CLI args (via argparse in main()) as function parameters,
which pytest doesn't understand. Provide fixtures with sensible defaults.
"""

import os
import tempfile
from pathlib import Path

import pytest


ONLINE_TEST_NODEIDS = (
    "dev/tests/test_agent_capabilities.py",
    "dev/tests/test_expert_io_format.py",
    "dev/tests/test_kernel_expert.py::test_kernel_expert_tool_calling",
    "dev/tests/test_semcode_mcp.py::test_semcode_mcp_binary_exists",
    "dev/tests/test_semcode_mcp.py::test_default_semcode_db_exists",
    "dev/tests/test_semcode_mcp.py::test_linux_next_semcode_db_exists",
    "dev/tests/test_test_expert.py::test_qemu_tool_calling",
    "dev/tests/test_tool_expert_mcp.py::test_tool_calling_loop",
    "dev/tests/test_tool_experts.py::test_expert_direct",
    "dev/tests/test_tool_experts.py::test_all_experts",
)


def pytest_addoption(parser):
    parser.addoption(
        "--run-online",
        action="store_true",
        default=False,
        help="run tests that call live LLM APIs or external crash sessions",
    )


def pytest_collection_modifyitems(config, items):
    skip_online = pytest.mark.skip(reason="online test skipped; use --run-online to run")
    run_online = config.getoption("--run-online")
    for item in items:
        if any(item.nodeid.startswith(pattern) for pattern in ONLINE_TEST_NODEIDS):
            item.add_marker(pytest.mark.online)
            if not run_online:
                item.add_marker(skip_online)


@pytest.fixture
def name() -> str:
    return "test"


@pytest.fixture
def kernel_path() -> str | None:
    return os.environ.get("TEST_KERNEL_PATH")


@pytest.fixture
def log_path() -> str | None:
    path = os.environ.get("TEST_LOG_PATH")
    if path:
        return path
    fd, tmp = tempfile.mkstemp(suffix=".log", prefix="test_boot_")
    with os.fdopen(fd, "w") as f:
        f.write("Linux version 5.15.0\n")
        f.write("Kernel panic - not syncing: hung_task\n")
        f.write("Call Trace:\n")
    return tmp


@pytest.fixture
def qemu_available() -> bool:
    return False


@pytest.fixture
def expert_type() -> str:
    return os.environ.get("TEST_EXPERT_TYPE", "crash_analysis")


@pytest.fixture
def state() -> dict:
    return {}
