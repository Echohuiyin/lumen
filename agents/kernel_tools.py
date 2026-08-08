"""Kernel Expert Tools - 文件操作和编译验证工具。

让内核专家能够实际创建文件、编译代码，而不只是生成文本描述。

工具列表：
- create_directory: 创建目录
- write_file: 写入文件内容
- read_file: 读取文件内容
- check_file_exists: 检查文件是否存在
- list_directory: 列出目录内容
- search_files: 使用 rg 搜索源码/输出文件
- bash: 运行受控 shell 命令
"""

from pathlib import Path
from typing import Optional
import os
import re
import subprocess

from langchain_core.tools import StructuredTool

from paths import PROJECT_ROOT


MAX_OUTPUT_CHARS = 20000
def _truncate_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... <truncated {len(text) - limit} chars>"


def _resolve_workdir(workdir: str | None = None) -> Path:
    """Resolve a command workdir and keep relative paths under PROJECT_ROOT."""
    if not workdir:
        return PROJECT_ROOT
    expanded = Path(os.path.expanduser(workdir))
    if not expanded.is_absolute():
        expanded = PROJECT_ROOT / expanded
    return expanded.resolve()


def create_directory(path: str) -> str:
    """Create directory at specified path.

    Args:
        path: Directory path to create (absolute or relative)

    Returns:
        Success message or error description
    """
    try:
        expanded_path = os.path.expanduser(path)
        Path(expanded_path).mkdir(parents=True, exist_ok=True)
        return f"✓ Directory created: {expanded_path}"
    except Exception as e:
        return f"✗ Error creating directory {path}: {str(e)}"


def write_file(file_path: str, content: str) -> str:
    """Write content to file.

    Args:
        file_path: Path to file (absolute or relative)
        content: Content to write

    Returns:
        Success message or error description
    """
    try:
        expanded_path = os.path.expanduser(file_path)
        with open(expanded_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✓ File written: {expanded_path} ({len(content)} chars)"
    except Exception as e:
        return f"✗ Error writing file {file_path}: {str(e)}"


def read_file(file_path: str) -> str:
    """Read file content.

    Args:
        file_path: Path to file (absolute or relative)

    Returns:
        File content or error description
    """
    try:
        expanded_path = os.path.expanduser(file_path)
        with open(expanded_path, "r", encoding="utf-8") as f:
            content = f.read()
        return f"✓ File read: {expanded_path}\n{content[:500]}..."
    except Exception as e:
        return f"✗ Error reading file {file_path}: {str(e)}"


def check_file_exists(file_path: str) -> str:
    """Check if file exists.

    Args:
        file_path: Path to file (absolute or relative)

    Returns:
        Existence status message
    """
    try:
        expanded_path = os.path.expanduser(file_path)
        exists = os.path.exists(expanded_path)
        return f"File {expanded_path}: {'✓ exists' if exists else '✗ not found'}"
    except Exception as e:
        return f"✗ Error checking file {file_path}: {str(e)}"


def list_directory(path: str = ".", max_entries: int = 200) -> str:
    """List directory contents.

    Args:
        path: Directory path to list
        max_entries: Maximum number of entries to return

    Returns:
        Directory listing or error description
    """
    try:
        target = _resolve_workdir(path)
        if not target.exists():
            return f"✗ Directory not found: {target}"
        if not target.is_dir():
            return f"✗ Not a directory: {target}"

        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        lines = [f"Directory: {target}"]
        for entry in entries[:max_entries]:
            kind = "dir " if entry.is_dir() else "file"
            size = "" if entry.is_dir() else f" {entry.stat().st_size} bytes"
            lines.append(f"{kind:4} {entry.name}{size}")
        if len(entries) > max_entries:
            lines.append(f"... <truncated {len(entries) - max_entries} entries>")
        return "\n".join(lines)
    except Exception as e:
        return f"✗ Error listing directory {path}: {str(e)}"


def search_files(pattern: str, path: str = ".", file_glob: Optional[str] = None, max_results: int = 100) -> str:
    """Search files using ripgrep.

    Args:
        pattern: Text or regex pattern to search
        path: Directory or file path to search
        file_glob: Optional rg glob, e.g. '*.c'
        max_results: Maximum matching lines to return

    Returns:
        Search results or error description
    """
    try:
        search_path = _resolve_workdir(path)
        if not search_path.exists():
            return f"✗ Search path not found: {search_path}"

        cmd = ["rg", "-n", "--no-heading", "--color", "never"]
        if file_glob:
            cmd.extend(["-g", file_glob])
        cmd.extend([pattern, str(search_path)])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout if result.stdout else result.stderr
        if result.returncode == 1:
            return f"No matches for {pattern!r} in {search_path}"
        if result.returncode != 0:
            return f"✗ rg failed (exit {result.returncode}):\n{_truncate_output(output)}"

        lines = output.splitlines()
        limited = "\n".join(lines[:max_results])
        if len(lines) > max_results:
            limited += f"\n... <truncated {len(lines) - max_results} matches>"
        return _truncate_output(limited)
    except subprocess.TimeoutExpired:
        return "✗ Search timeout (>30s)"
    except FileNotFoundError:
        return "✗ rg not found; install ripgrep or use bash with grep as fallback"
    except Exception as e:
        return f"✗ Error searching files: {str(e)}"


def bash(command: str, workdir: str = ".", timeout: int = 60) -> str:
    """Run a controlled shell command.

    Args:
        command: Shell command to run
        workdir: Working directory, project-relative by default
        timeout: Timeout in seconds, capped at 300

    Returns:
        Command output with return code, stdout, and stderr
    """
    blocked = _is_blocked_command(command)
    if blocked:
        return f"✗ Command blocked by safety policy (pattern: {blocked})"

    try:
        cwd = _resolve_workdir(workdir)
        if not cwd.exists() or not cwd.is_dir():
            return f"✗ Invalid working directory: {cwd}"
        if not _is_relative_to(cwd, PROJECT_ROOT):
            return f"✗ Workdir outside project is not allowed for bash: {cwd}"

        safe_timeout = max(1, min(int(timeout), MAX_BASH_TIMEOUT))
        result = subprocess.run(
            ["bash", "-lc", command],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=safe_timeout,
        )

        output = (
            f"Command: {command}\n"
            f"Workdir: {cwd}\n"
            f"Return code: {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        return _truncate_output(output)
    except subprocess.TimeoutExpired:
        return f"✗ Command timeout (>{timeout}s): {command}"
    except Exception as e:
        return f"✗ Error running command {shlex.quote(command)}: {str(e)}"


def create_kernel_tools() -> list:
    """Create kernel expert tool set.

    Returns:
        List of StructuredTool instances
    """
    tools = [
        StructuredTool.from_function(
            name="create_directory",
            func=create_directory,
            description="Create directory at specified path",
        ),
        StructuredTool.from_function(
            name="write_file",
            func=write_file,
            description="Write content to file",
        ),
        StructuredTool.from_function(
            name="read_file",
            func=read_file,
            description="Read file content",
        ),
        StructuredTool.from_function(
            name="check_file_exists",
            func=check_file_exists,
            description="Check if file exists",
        ),
        StructuredTool.from_function(
            name="list_directory",
            func=list_directory,
            description="List directory contents with stable, truncated output",
        ),
        StructuredTool.from_function(
            name="search_files",
            func=search_files,
            description="Search files using ripgrep; prefer this over bash grep for code search",
        ),
    ]
    return tools
