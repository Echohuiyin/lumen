"""Online smoke test for the Kernel Expert Claude Code backend."""

import json
import os
import shutil
from pathlib import Path

import pytest

from agents.backends import ClaudeCodeBackend


@pytest.mark.online
def test_kernel_expert_claude_code_workdir_contract(tmp_path):
    """Verify the local Claude Code CLI accepts the agent-loop invocation contract."""
    cli = os.environ.get("LUMEN_CLAUDE_COMMAND", "claude")
    if shutil.which(cli) is None:
        pytest.skip("local Claude Code CLI is not installed")

    settings = os.environ.get("LUMEN_CLAUDE_SETTINGS_FILE", "~/.claude/settings.json")
    settings_path = Path(os.path.expanduser(settings))
    if not settings_path.is_file():
        pytest.skip(f"Claude settings file is missing: {settings_path}")
    kernel_source = Path(os.path.expanduser(
        os.environ.get("LUMEN_KERNEL_SOURCE", "~/linux-next")
    ))
    if not kernel_source.is_dir():
        pytest.skip(f"kernel source directory is missing: {kernel_source}")

    backend = ClaudeCodeBackend(
        cli_command=cli,
        cli_timeout=120,
        model=os.environ.get("LUMEN_CLAUDE_MODEL", "sonnet"),
        permission_mode="bypassPermissions",
        max_turns=10,
        settings_file=str(settings_path),
        disable_skills=True,
    )
    response = backend.invoke(
        [
            {
                "role": "user",
                "content": (
                    "使用 Read 确认目标源码目录中的 Makefile 存在，然后只回复一行 JSON："
                    "{\"status\":\"ok\",\"workdir\":true,\"kernel_source\":true}。"
                    "不要写入或修改任何文件。"
                ),
            }
        ],
        workdir=str(tmp_path),
        add_dirs=[str(tmp_path), str(kernel_source)],
    )
    payload = json.loads(response.content.strip())
    assert payload == {"status": "ok", "workdir": True, "kernel_source": True}
