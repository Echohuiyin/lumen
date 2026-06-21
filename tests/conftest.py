"""Pytest fixtures for tests originally designed as standalone scripts.

These tests use CLI args (via argparse in main()) as function parameters,
which pytest doesn't understand. Provide fixtures with sensible defaults.
"""

import os
import tempfile
from pathlib import Path

import pytest


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
