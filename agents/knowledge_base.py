import os
import subprocess
import re
from pathlib import Path
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm_display import call_llm_with_display, set_session_dir
from llm_config import get_llm_with_config, load_prompt_from_file, PROJECT_ROOT
from graph.rn_state import MaintenanceWorkflowState
from paths import resolve_best_skill_path, ANALYSIS_SKILL_PATH


def knowledge_base_node(state: MaintenanceWorkflowState) -> dict:
    """知识库生成 agent：将问题总结并形成知识库文件进行归档，并自动导入 Chroma 向量数据库。"""
    set_session_dir(state.get("session_dir"))
    config = state.get("config", {})
    agent_config = config.get("agents", {}).get("knowledge_base", {})
    default_config = config.get("default", {})
    llm = get_llm_with_config(agent_config, default_config=default_config, agent_name="knowledge_base")
    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", "prompts/knowledge_base.md")
    )

    # 汇总所有分析结果
    expert_results = state.get("expert_results", [])
    all_paths = state.get("all_possible_paths", [])
    max_path = state.get("max_likely_path", "")
    # Keep the raw sections visible even for legacy/retry states whose
    # structured fields were not populated by the model.
    raw_kernel_analysis = state.get("kernel_analysis", "")
    if not all_paths:
        all_paths = _extract_path_lines(raw_kernel_analysis, "ALL_POSSIBLE_PATHS")
    if not max_path:
        max_path = _extract_path_section(raw_kernel_analysis, "MAX_LIKELY_PATH")
    expert_summaries = []
    for result in expert_results:
        expert_summaries.append(
            f"### {result['expert_name']}（{result['expert_type']}）\n{result['analysis_output']}"
        )

    user_content = (
        f"用户输入:\n{state['user_input']}\n\n"
        f"## 结构化输入校验\n{state.get('validation_contract', {})}\n\n"
        f"## 工具专家分析结果\n" + "\n\n".join(expert_summaries) + "\n\n"
        f"## 内核专家分析\n{state.get('kernel_analysis', '')}\n\n"
        f"## UAF/引用计数路径分析（必须原样保留）\n"
        f"所有可能路径：\n{_format_paths(all_paths)}\n\n"
        f"最大可能路径：\n{max_path}\n\n"
        f"## 结构化内核专家契约\n{state.get('kernel_contract', {})}\n\n"
        f"## 复现用例\n{state.get('reproduce_case', '')}\n\n"
        f"## 内核维测方案\n{state.get('kernel_diagnosis', '')}\n\n"
        f"## 结构化测试契约\n"
        f"- Target arch: {state.get('target_arch', '')}\n"
        f"- Boot kernel: {state.get('boot_kernel_path', '')}\n"
        f"- Reproducer dir: {state.get('reproducer_dir', '')}\n"
        f"- Reproducer module: {state.get('reproducer_module_path', '')}\n"
        f"- Test script: {state.get('test_script_path', '')}\n"
        f"- Expected signal: {state.get('expected_signal', '')}\n\n"
        f"## 测试验证结果\n{state.get('test_result', '')}\n\n"
        f"## 结构化测试结果\n{state.get('test_contract', {})}\n\n"
        f"请将以上内容总结为知识库文档。"
    )

    try:
        response = call_llm_with_display(
            "知识库生成", "总结归档", llm,
            [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
        )
        knowledge_content = response.content.strip()
    except Exception as e:
        # knowledge_base is the terminal node — its LLM failure must not
        # throw away the entire workflow's analysis. Degrade to saving the
        # raw structured summary so the case is still archived and
        # retrievable. Common triggers: 429 budget_exceeded on the proxy,
        # upstream 5xx, or transient network errors.
        err_line = f"[knowledge_base LLM failure] {type(e).__name__}: {str(e)[:300]}"
        knowledge_content = (
            f"# 知识库归档（LLM 总结失败，已降级保存原始输入）\n\n"
            f"{err_line}\n\n"
            f"--- 原始输入 ---\n{user_content}"
        )

    # 保存知识库文件
    knowledge_file = _save_knowledge_file(state, knowledge_content, config)

    # 自动导入到 Chroma 向量数据库
    import_success, import_message = _import_to_chroma(knowledge_file)

    issue_id = state.get("issue_id", "")
    issue_url = state.get("issue_url", "")

    test_passed = state.get("test_passed", False)
    status_text = "成功复现" if test_passed else "未成功复现，已归档分析过程和改进建议"

    # 构建最终响应
    final_response = (
        f"问题分析已完成（{status_text}）。\n\n"
        f"Issue: {issue_id} ({issue_url})\n"
        f"知识库文件: {knowledge_file}\n\n"
        f"Chroma 导入: {import_message}\n\n"
        f"## UAF/引用计数路径分析\n"
        f"所有可能路径：\n{_format_paths(all_paths)}\n\n"
        f"最大可能路径：\n{max_path or '未明确，需结合归档中的完整分析继续排查。'}\n\n"
        f"共调用 {len(expert_results)} 个工具专家，"
        f"测试验证 {state.get('test_attempts', 0)} 次。"
    )

    return {
        "knowledge_file": knowledge_file,
        "final_response": final_response,
    }


def _format_paths(paths) -> str:
    """Render path findings without allowing an empty list to erase evidence."""
    if not paths:
        return "未提取到结构化路径列表；请以知识库中的内核专家原始分析为准。"
    if isinstance(paths, str):
        return paths
    return "\n".join(f"{i}. {path}" for i, path in enumerate(paths, 1))


def _extract_path_section(text: str, marker: str) -> str:
    match = re.search(rf"^{re.escape(marker)}:\s*$([\s\S]*?)(?=^[A-Z_]+:|\Z)", text or "", re.MULTILINE)
    return match.group(1).strip() if match else ""


def _extract_path_lines(text: str, marker: str) -> list[str]:
    section = _extract_path_section(text, marker)
    return [line.strip() for line in section.splitlines() if line.strip()]


def _save_knowledge_file(state: MaintenanceWorkflowState, content: str, config: dict) -> str:
    """将知识库内容保存为文件（优先 session 目录）。"""
    session_dir = state.get("session_dir")
    if session_dir:
        output_path = Path(session_dir)
    else:
        kb_config = config.get("knowledge_base", {})
        output_dir = kb_config.get("output_dir", "knowledge_base")
        output_path = PROJECT_ROOT / output_dir

    output_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    issue_id = state.get("issue_id", "unknown")
    filename = f"knowledge_{issue_id}_{timestamp}.md"

    file_path = output_path / filename
    file_path.write_text(content, encoding="utf-8")

    return str(file_path)


def _import_to_chroma(knowledge_file: str) -> tuple[bool, str]:
    """将知识库文件导入到 Chroma 向量数据库。

    使用 rag-case-retrieval skill 的 import_cases.py 脚本。
    需要先将 markdown 转换为 JSON 格式。

    Returns:
        (success, message) - 是否成功及消息
    """
    import json
    from pathlib import Path

    skill_path = resolve_best_skill_path("rag-case-retrieval")
    if skill_path is None:
        return False, "rag-case-retrieval skill 未找到，无法导入 Chroma"

    import_script = skill_path / "scripts" / "import_cases.py"
    if not import_script.exists():
        return False, f"导入脚本未找到: {import_script}"

    venv_python = ANALYSIS_SKILL_PATH / ".venv" / "bin" / "python"
    if not venv_python.exists():
        return False, f"Python venv 未找到: {venv_python}"

    # 读取知识库文件内容
    try:
        kb_content = Path(knowledge_file).read_text(encoding="utf-8")
        kb_filename = Path(knowledge_file).stem
    except Exception as e:
        return False, f"读取知识库文件失败: {str(e)}"

    # Resolve to absolute path — subprocess runs with a different cwd
    knowledge_file = str(Path(knowledge_file).resolve())

    # 创建临时 JSON 文件用于导入
    temp_json = Path(knowledge_file).with_suffix(".json")
    case_data = {
        "id": kb_filename,
        "title": kb_filename,
        "content": kb_content,
        "metadata": {
            "source": "maintenance_workflow",
            "created_at": datetime.now().isoformat(),
        }
    }

    # 创建符合 import_cases.py 期望的 JSON 格式
    json_data = {
        "cases": [case_data]
    }

    try:
        temp_json.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")

        # 调用导入脚本
        result = subprocess.run(
            [
                str(venv_python),
                str(import_script),
                "--source", "json",
                "--file", str(temp_json),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(skill_path / "scripts"),
        )

        # 清理临时文件
        temp_json.unlink(missing_ok=True)

        if result.returncode == 0:
            return True, f"✓ 成功导入 ({kb_filename})"
        else:
            error_msg = (result.stderr or result.stdout or "")[:500]
            return False, f"✗ 导入失败: {error_msg}"

    except subprocess.TimeoutExpired:
        temp_json.unlink(missing_ok=True)
        return False, "导入超时 (60s)"
    except Exception as e:
        temp_json.unlink(missing_ok=True)
        return False, f"导入异常: {str(e)}"
