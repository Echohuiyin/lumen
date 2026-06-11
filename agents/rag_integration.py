"""RAG 搜索集成模块：执行实际的向量检索并返回结构化结果。"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def retrieve_similar_cases(query: str, top_k: int = 3, min_similarity: float = 0.3) -> dict:
    """执行 RAG 搜索并返回结构化结果。

    Args:
        query: 搜索查询文本
        top_k: 返回结果数量
        min_similarity: 最小相似度阈值

    Returns:
        包含检索结果的字典，格式：
        {
            "status": "success" | "error" | "no_results",
            "query": str,
            "results": [
                {
                    "id": str,
                    "title": str,
                    "content": str,
                    "similarity_score": float,
                    "metadata": dict
                }
            ],
            "error": str (如果失败)
        }
    """
    # 获取 rag-case-retrieval skill 的脚本路径
    skill_path = Path.home() / ".claude" / "skills" / "rag-case-retrieval"
    script_path = skill_path / "scripts" / "retrieve_cases.py"

    # 使用 Analysis-SKILL 的 venv（包含 chromadb）
    venv_python = Path.home() / "code" / "Analysis-SKILL" / ".venv" / "bin" / "python"

    if not script_path.exists():
        return {
            "status": "error",
            "query": query,
            "results": [],
            "error": f"RAG script not found at {script_path}"
        }

    if not venv_python.exists():
        return {
            "status": "error",
            "query": query,
            "results": [],
            "error": f"Python venv not found at {venv_python}"
        }

    try:
        # 执行检索脚本
        result = subprocess.run(
            [
                str(venv_python),
                str(script_path),
                query,
                "--top-k", str(top_k),
                "--min-similarity", str(min_similarity),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            return {
                "status": "error",
                "query": query,
                "results": [],
                "error": result.stderr or "Unknown error"
            }

        # 解析 JSON 输出（脚本输出中最后部分是 JSON）
        output = result.stdout
        # 找到 JSON 部分（以 { 开头的行）
        lines = output.strip().split("\n")
        json_start = -1
        for i, line in enumerate(lines):
            if line.strip().startswith("{"):
                json_start = i
                break

        if json_start >= 0:
            json_content = "\n".join(lines[json_start:])
            data = json.loads(json_content)
            return {
                "status": data.get("status", "success"),
                "query": query,
                "results": data.get("results", []),
                "summary": data.get("summary", {}),
                "error": None
            }
        else:
            return {
                "status": "no_results",
                "query": query,
                "results": [],
                "error": "No JSON output found"
            }

    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "query": query,
            "results": [],
            "error": "RAG search timed out"
        }
    except json.JSONDecodeError as e:
        return {
            "status": "error",
            "query": query,
            "results": [],
            "error": f"JSON parse error: {e}"
        }
    except Exception as e:
        return {
            "status": "error",
            "query": query,
            "results": [],
            "error": str(e)
        }


def format_rag_results_for_prompt(results: dict) -> str:
    """将 RAG 结果格式化为可嵌入 prompt 的文本。

    Args:
        results: retrieve_similar_cases 返回的结果字典

    Returns:
        格式化的文本，可直接插入 LLM prompt
    """
    if results["status"] == "error":
        return f"### RAG 搜索失败\n错误: {results['error']}"

    if results["status"] == "no_results" or not results["results"]:
        return "### RAG 搜索结果\n未找到相似度高于阈值的历史案例。"

    output = "### RAG 搜索结果\n"
    output += f"查询: `{results['query']}`\n"
    output += f"找到 {len(results['results'])} 条相似案例:\n\n"

    for i, case in enumerate(results["results"], 1):
        similarity = case.get("similarity_score", 0)
        title = case.get("title", "无标题")
        content = case.get("content", "")
        metadata = case.get("metadata", {})

        # 截取内容摘要（前500字符）
        content_preview = content[:500] + "..." if len(content) > 500 else content

        output += f"#### [{i}] {title}\n"
        output += f"**相似度**: {similarity:.2f}\n"
        if metadata:
            category = metadata.get("category", "")
            if category:
                output += f"**分类**: {category}\n"
        output += f"**内容摘要**:\n```\n{content_preview}\n```\n\n"

    return output


def get_rag_context_for_query(query: str, top_k: int = 3) -> str:
    """便捷函数：执行 RAG 搜索并返回格式化上下文。

    用于在 tool_expert 中直接获取 RAG 结果并嵌入分析。
    """
    results = retrieve_similar_cases(query, top_k=top_k)
    return format_rag_results_for_prompt(results)