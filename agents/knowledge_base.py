import os
import subprocess
import re
import tempfile
import json
from pathlib import Path
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm_display import call_llm_with_display, set_session_dir
from agents.error_handling import classify_error
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
    kernel_contract = state.get("kernel_contract") or {}
    semcode_path_analysis = state.get("semcode_path_analysis") or {}
    round_summary = _summarize_test_rounds(state.get("test_rounds", []))
    # State fields are maintained for compatibility, but kernel_contract is
    # the durable handoff and may be recovered directly from disk.
    if not all_paths:
        all_paths = kernel_contract.get("all_possible_paths", []) or []
    if not max_path:
        max_path = kernel_contract.get("max_likely_path", "") or ""
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
        f"## semcode P2 事件图（原始结果，必须保留）\n{semcode_path_analysis}\n\n"
        f"## 结构化内核专家契约\n{kernel_contract}\n\n"
        f"## 复现用例\n{state.get('reproduce_case', '')}\n\n"
        f"## 内核维测方案\n{state.get('kernel_diagnosis', '')}\n\n"
        f"## 结构化测试契约\n"
        f"- Target arch: {state.get('target_arch', '')}\n"
        f"- Boot kernel: {state.get('boot_kernel_path', '')}\n"
        f"- Reproducer dir: {state.get('reproducer_dir', '')}\n"
        f"- Reproducer module: {state.get('reproducer_module_path', '')}\n"
        f"- Execution steps: {kernel_contract.get('execution_steps', [])}\n"
        f"- Expected signal: {state.get('expected_signal', '')}\n\n"
        f"## 测试验证结果\n{state.get('test_result', '')}\n\n"
        f"## 各复现轮次（每轮仅一句话）\n{round_summary}\n\n"
        f"未复现时，请在‘复现结果’下简要列出各轮已做的构造复现尝试及其确定性失败原因；不得新增建议。\n\n"
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
        error = classify_error(e, operation="knowledge_base LLM summary")
        err_line = (
            f"[knowledge_base LLM failure] {error.category}/{error.code}: {error.message}. "
            f"Next action: {error.next_action} Cause: {error.cause}"
        )
        knowledge_content = (
            f"# 知识库归档（LLM 总结失败，已降级保存原始输入）\n\n"
            f"{err_line}\n\n"
            f"--- 原始输入 ---\n{user_content}"
        )

    # Do not delegate the evidence appendix to the LLM.  The report remains
    # useful even when it summarises poorly or its output is truncated.
    path_appendix = _render_path_analysis_appendix(
        all_paths=all_paths,
        max_path=max_path,
        kernel_contract=kernel_contract,
        semcode_path_analysis=semcode_path_analysis,
    )
    knowledge_content = f"{knowledge_content.rstrip()}\n\n{path_appendix}\n"

    # 保存知识库文件
    knowledge_file = _save_knowledge_file(state, knowledge_content, config)

    # 自动导入到 Chroma 向量数据库
    import_success, import_message = _import_to_chroma(knowledge_file)

    issue_id = state.get("issue_id", "")
    issue_url = state.get("issue_url", "")

    test_passed = state.get("test_passed", False)
    status_text = "成功复现" if test_passed else "未成功复现"

    # 构建最终响应
    final_response = (
        f"问题分析已完成（{status_text}）。\n\n"
        f"Issue: {issue_id} ({issue_url})\n"
        f"知识库文件: {knowledge_file}\n\n"
        f"Chroma 导入: {import_message}\n\n"
        f"{path_appendix}\n\n"
        f"共调用 {len(expert_results)} 个工具专家，"
        f"完成 {len(state.get('test_rounds', []) or [])} 个复现闭环，最终轮次为 {state.get('test_attempts', 0)}。"
    )

    return {
        "knowledge_file": knowledge_file,
        "final_response": final_response,
    }


def _summarize_test_rounds(rounds) -> str:
    """Render one compact, factual sentence per reproduction attempt."""
    if not rounds:
        return "无已执行的复现轮次。"
    lines = []
    for index, item in enumerate(rounds, 1):
        if not isinstance(item, dict):
            lines.append(f"第{index}轮：结果记录格式无效，未判定为成功。")
            continue
        status = item.get("status") or item.get("result") or item.get("code") or "未知"
        summary = str(item.get("summary") or item.get("message") or "").replace("\n", " ").strip()
        if len(summary) > 160:
            summary = summary[:157] + "..."
        lines.append(f"第{item.get('round', index)}轮：{status}；{summary or '无摘要'}。")
    return "\n".join(lines)


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


def _render_path_analysis_appendix(
    *, all_paths, max_path: str, kernel_contract: dict,
    semcode_path_analysis: dict | None = None,
) -> str:
    """Render a deterministic P0 appendix for both archive and CLI output."""
    required = bool(kernel_contract.get("path_analysis_required"))
    scope = kernel_contract.get("path_analysis_scope") or {}
    excluded = kernel_contract.get("excluded_paths") or []
    reproduction_target = kernel_contract.get("reproduction_target_path") or ""
    rationale = kernel_contract.get("max_likely_path_rationale") or ""
    lines = ["## UAF/引用计数路径分析（确定性附录）"]
    if not required and not all_paths and not max_path:
        lines.append("本案例未声明 UAF/引用计数路径分析要求。")
        return "\n".join(lines)

    lines += ["", "### 分析范围"]
    scope_labels = (
        ("kernel_commit", "Kernel commit"),
        ("kernel_config", "Kernel config"),
        ("entry_points", "入口"),
        ("object_type", "对象类型"),
        ("concurrency_model", "并发模型"),
    )
    for key, label in scope_labels:
        value = scope.get(key, "")
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        lines.append(f"- {label}: {value or '未提供'}")

    lines += ["", "### 所有可能路径", _format_paths(all_paths)]
    lines += ["", "### 最大可能路径", max_path or "未明确"]
    lines.append(f"- 选择依据: {rationale or '未提供'}")
    lines.append(f"- 复现目标路径: {reproduction_target or '未提供'}")
    lines += ["", "### 已排除路径"]
    if not excluded:
        lines.append("- 无；或未提供排除依据。")
    else:
        for item in excluded:
            if not isinstance(item, dict):
                lines.append(f"- {item}")
                continue
            lines.append(f"- {item.get('path', '未命名路径')}：{item.get('rationale', '未提供依据')}")
    structured = kernel_contract.get("uaf_analysis")
    if structured:
        lines += ["", "### 原始结构化路径 Contract", "```json", json.dumps(structured, ensure_ascii=False, indent=2), "```"]
    if semcode_path_analysis:
        lines += [
            "", "### semcode P2 原始事件图结果", "```json",
            json.dumps(semcode_path_analysis, ensure_ascii=False, indent=2), "```",
        ]
    return "\n".join(lines)


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
    _atomic_write_text(file_path, content)

    return str(file_path)


def _atomic_write_text(file_path: Path, content: str) -> None:
    """Publish an archive only after its complete UTF-8 content is written."""
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=file_path.parent,
        prefix=f".{file_path.name}.", suffix=".tmp", delete=False,
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)
    try:
        temp_path.replace(file_path)
    finally:
        temp_path.unlink(missing_ok=True)


def _import_to_chroma(knowledge_file: str) -> tuple[bool, str]:
    """将知识库文件导入到 Chroma 向量数据库。

    使用 rag-case-retrieval skill 的 import_cases.py 脚本。
    需要先将 markdown 转换为 JSON 格式。

    Returns:
        (success, message) - 是否成功及消息
    """
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
