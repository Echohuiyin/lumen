"""Online smoke test for the Kernel Expert Codex backend."""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from agents.backends import CodexBackend


@pytest.mark.online
def test_kernel_expert_codex_workdir_contract():
    """Verify Codex can read kernel source while writing only to session output."""
    cli = os.environ.get("LUMEN_CODEX_CLI", "codex")
    if shutil.which(cli) is None:
        pytest.skip("Codex CLI is not installed")

    project_root = Path(__file__).resolve().parents[2]
    runtime_home = Path(
        os.environ.get("LUMEN_CODEX_RUNTIME_HOME", project_root / "runtime" / "codex-home")
    ).expanduser().resolve()
    auth = runtime_home / ".codex" / "auth.json"
    if not auth.is_file() and not os.environ.get("CODEX_API_KEY"):
        pytest.skip(f"isolated Codex authentication is missing: {auth}")

    kernel_source_value = os.environ.get("LUMEN_KERNEL_SOURCE")
    if not kernel_source_value:
        pytest.skip("LUMEN_KERNEL_SOURCE is not configured")
    kernel_source = Path(kernel_source_value).expanduser().resolve()
    if not kernel_source.is_dir():
        pytest.skip(f"kernel source directory is missing: {kernel_source}")

    semcode = Path(
        os.environ.get(
            "LUMEN_SEMCODE_MCP",
            project_root / "Analysis-SKILL" / "tools" / "semcode" / "target" / "release" / "semcode-mcp",
        )
    ).expanduser().resolve()
    if not semcode.is_file():
        pytest.skip(f"Semcode MCP is missing: {semcode}")

    backend = CodexBackend(
        cli_command=cli,
        cli_timeout=180,
        model=os.environ.get("LUMEN_CODEX_MODEL", ""),
        runtime_home=str(runtime_home),
        project_root=str(project_root),
        project_skills_dir=str(project_root / ".agents" / "skills"),
        semcode_mcp={"command": str(semcode), "args": ["-d", str(kernel_source / ".semcode.db")]},
    )
    runtime_dir = project_root / "runtime"
    runtime_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codex-online-", dir=runtime_dir) as workdir:
        response = backend.invoke(
            [
                HumanMessage(
                    content=(
                        f"Read {kernel_source / 'Makefile'} and verify it exists. "
                        "Do not modify any file. Reply only with "
                        '{"status":"ok","kernel_source":true}.'
                    )
                )
            ],
            workdir=workdir,
            add_dirs=[str(kernel_source)],
        )
    assert json.loads(response.content.strip()) == {"status": "ok", "kernel_source": True}
