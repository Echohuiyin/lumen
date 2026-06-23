"""路径解析模块：统一处理项目和子模块路径。

所有路径基于 PROJECT_ROOT 动态解析，避免硬编码。

可通过环境变量覆盖默认路径：
    LUMEN_OUTPUT_DIR    复现器输出目录（默认 outputs/）
"""

from pathlib import Path
import os

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent

# 复现器输出目录（可通过环境变量覆盖）
_OUTPUT_DIR_OVERRIDE = os.environ.get("LUMEN_OUTPUT_DIR")
OUTPUT_DIR = Path(_OUTPUT_DIR_OVERRIDE) if _OUTPUT_DIR_OVERRIDE else (PROJECT_ROOT / "outputs")

# Analysis-SKILL 子模块路径（git submodule）
ANALYSIS_SKILL_PATH = PROJECT_ROOT / "Analysis-SKILL"

# AICRASHER MCP Server 源码路径
AICRASHER_SRC_PATH = ANALYSIS_SKILL_PATH / "src"

# Skills 目录路径
SKILLS_PATH = ANALYSIS_SKILL_PATH / "skills"


def resolve_skill_path(skill_name: str) -> Path:
    """解析 Analysis-SKILL 子项目中的 skill 目录路径。

    Args:
        skill_name: Skill 名称，如 "kernel-fault-injection", "kernel-build"

    Returns:
        Skill 目录的绝对路径
    """
    return SKILLS_PATH / skill_name


def resolve_analysis_skill_path() -> Path:
    """解析 Analysis-SKILL 子模块根目录路径。

    Returns:
        Analysis-SKILL 目录的绝对路径
    """
    return ANALYSIS_SKILL_PATH


def resolve_aicrasher_path() -> Path:
    """解析 aicrasher MCP Server 源码路径。

    Returns:
        aicrasher src 目录的绝对路径
    """
    return AICRASHER_SRC_PATH


def get_skill_path_candidates(skill_name: str) -> list[Path]:
    """获取 skill 目录的候选路径列表。

    按优先级返回可能的 skill 安装位置：
    1. Analysis-SKILL 子模块（推荐）
    2. ~/.claude/skills（Claude Code 默认安装位置）
    3. ~/.codebuddy/skills（Codebuddy 安装位置）

    Args:
        skill_name: Skill 名称

    Returns:
        候选路径列表（存在的路径优先）
    """
    candidates = [
        resolve_skill_path(skill_name),
        Path.home() / ".claude" / "skills" / skill_name,
        Path.home() / ".codebuddy" / "skills" / skill_name,
    ]

    # 存在的路径排在前面
    existing = [p for p in candidates if p.exists()]
    non_existing = [p for p in candidates if not p.exists()]

    return existing + non_existing


def resolve_best_skill_path(skill_name: str) -> Path | None:
    """解析最佳 skill 路径（优先返回存在的路径）。

    Args:
        skill_name: Skill 名称

    Returns:
        存在的 skill 路径，或 None（如果都不存在）
    """
    for path in get_skill_path_candidates(skill_name):
        if path.exists():
            return path
    return None