import re
import subprocess
from pathlib import Path

from langchain_core.tools import tool

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ALLOWED_COMMAND_PREFIXES = (
    "git ",
    "pytest",
    "ruff ",
    "python ",
    "python3 ",
    "ls ",
    "ls",
    "cat ",
    "find ",
    "grep ",
    "wc ",
    "tree ",
    "pip ",
    "pip3 ",
)


def _resolve_path(path: str) -> Path:
    resolved = (PROJECT_ROOT / path).resolve()
    if not str(resolved).startswith(str(PROJECT_ROOT.resolve())):
        raise ValueError(f"路径超出项目范围: {path}")
    return resolved


@tool
def read_file(path: str) -> str:
    """读取项目内文件内容。path 为相对于项目根目录的路径。"""
    file_path = _resolve_path(path)
    if not file_path.is_file():
        return f"错误: 文件不存在 - {path}"
    return file_path.read_text(encoding="utf-8")


@tool
def write_file(path: str, content: str) -> str:
    """写入或覆盖项目内文件。path 为相对于项目根目录的路径。"""
    file_path = _resolve_path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return f"成功写入文件: {path} ({len(content)} 字符)"


@tool
def run_shell_command(command: str) -> str:
    """执行 shell 命令（仅限白名单前缀：git, pytest, ruff, python, ls, cat, find, grep, wc, tree, pip）。"""
    command = command.strip()
    if not any(command.startswith(prefix) for prefix in ALLOWED_COMMAND_PREFIXES):
        allowed = ", ".join(sorted(set(p.strip() for p in ALLOWED_COMMAND_PREFIXES)))
        return f"错误: 命令不在白名单中。允许的前缀: {allowed}"

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = result.stdout or result.stderr or "(无输出)"
        if result.returncode != 0:
            return f"命令退出码 {result.returncode}:\n{output}"
        return output
    except subprocess.TimeoutExpired:
        return "错误: 命令执行超时（60秒）"
    except Exception as exc:
        return f"错误: {exc}"


@tool
def search_code(pattern: str, file_glob: str = "**/*") -> str:
    """在项目中搜索关键词或正则表达式。file_glob 默认为所有文件。"""
    matches: list[str] = []
    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(re.escape(pattern))

    for file_path in PROJECT_ROOT.glob(file_glob):
        if not file_path.is_file():
            continue
        if any(part.startswith(".") for part in file_path.relative_to(PROJECT_ROOT).parts):
            continue
        try:
            text = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                rel = file_path.relative_to(PROJECT_ROOT)
                matches.append(f"{rel}:{line_no}: {line.strip()}")

    if not matches:
        return f"未找到匹配 '{pattern}' 的内容"
    if len(matches) > 50:
        return "\n".join(matches[:50]) + f"\n... 还有 {len(matches) - 50} 条匹配"
    return "\n".join(matches)


PROJECT_TOOLS = [read_file, write_file, run_shell_command, search_code]
