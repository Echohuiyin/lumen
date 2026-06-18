"""Kernel Expert Tools - 文件操作和编译验证工具。

让内核专家能够实际创建文件、编译代码，而不只是生成文本描述。

工具列表：
- create_directory: 创建目录
- write_file: 写入文件内容
- read_file: 读取文件内容
- compile_module: 编译内核模块
- check_file_exists: 检查文件是否存在
"""

from pathlib import Path
from typing import Optional
import os
import subprocess

from langchain_core.tools import StructuredTool


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


def compile_module(module_dir: str, kernel_dir: Optional[str] = None) -> str:
    """Compile kernel module using make.

    Args:
        module_dir: Directory containing module source and Makefile
        kernel_dir: Optional kernel build directory (defaults to /lib/modules/$(uname -r)/build)

    Returns:
        Compilation output (success or error log)
    """
    try:
        expanded_dir = os.path.expanduser(module_dir)

        # 确保使用绝对路径
        if not os.path.isabs(expanded_dir):
            expanded_dir = os.path.abspath(expanded_dir)

        if kernel_dir:
            kdir = os.path.expanduser(kernel_dir)
        else:
            kdir = f"/lib/modules/{os.uname().release}/build"

        cmd = ["make", "-C", kdir, f"M={expanded_dir}", "modules"]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )

        output = f"Command: {cmd}\n"
        output += f"Return code: {result.returncode}\n"
        output += f"stdout:\n{result.stdout}\n"
        output += f"stderr:\n{result.stderr}\n"

        if result.returncode == 0:
            output += "✓ Compilation successful"
        else:
            output += "✗ Compilation failed"

        return output
    except subprocess.TimeoutExpired:
        return "✗ Compilation timeout (>60s)"
    except Exception as e:
        return f"✗ Error compiling module: {str(e)}"


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
            name="compile_module",
            func=compile_module,
            description="Compile kernel module using make",
        ),
        StructuredTool.from_function(
            name="check_file_exists",
            func=check_file_exists,
            description="Check if file exists",
        ),
    ]
    return tools