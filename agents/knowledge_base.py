import os
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm_display import call_llm_with_display
from config import get_llm_with_config, load_prompt_from_file, PROJECT_ROOT
from graph.rn_state import MaintenanceWorkflowState


def knowledge_base_node(state: MaintenanceWorkflowState) -> dict:
    """知识库生成 agent：将问题总结并形成知识库文件进行归档。"""
    config = state.get("config", {})
    agent_config = config.get("agents", {}).get("knowledge_base", {})
    llm = get_llm_with_config(agent_config)
    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", "prompts/maintenance/knowledge_base.md")
    )

    # 汇总所有分析结果
    expert_results = state.get("expert_results", [])
    expert_summaries = []
    for result in expert_results:
        expert_summaries.append(
            f"### {result['expert_name']}（{result['expert_type']}）\n{result['analysis_output']}"
        )

    user_content = (
        f"用户输入:\n{state['user_input']}\n\n"
        f"## 工具专家分析结果\n" + "\n\n".join(expert_summaries) + "\n\n"
        f"## 内核专家分析\n{state.get('kernel_analysis', '')}\n\n"
        f"## 复现用例\n{state.get('reproduce_case', '')}\n\n"
        f"## 内核维测方案\n{state.get('kernel_diagnosis', '')}\n\n"
        f"## 测试验证结果\n{state.get('test_result', '')}\n\n"
        f"请将以上内容总结为知识库文档。"
    )

    response = call_llm_with_display(
        "知识库生成", "总结归档", llm,
        [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
    )

    # 保存知识库文件
    knowledge_content = response.content.strip()
    knowledge_file = _save_knowledge_file(state, knowledge_content, config)

    issue_id = state.get("issue_id", "")
    issue_url = state.get("issue_url", "")

    return {
        "knowledge_file": knowledge_file,
        "final_response": (
            f"问题分析已完成！\n\n"
            f"Issue: {issue_id} ({issue_url})\n"
            f"知识库文件: {knowledge_file}\n\n"
            f"共调用 {len(expert_results)} 个工具专家，"
            f"测试验证 {state.get('test_attempts', 0)} 次后成功复现。"
        ),
    }


def _save_knowledge_file(state: MaintenanceWorkflowState, content: str, config: dict) -> str:
    """将知识库内容保存为文件。"""
    kb_config = config.get("knowledge_base", {})
    output_dir = kb_config.get("output_dir", "knowledge_base")

    output_path = PROJECT_ROOT / output_dir
    output_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    issue_id = state.get("issue_id", "unknown")
    filename = f"{issue_id}_{timestamp}.md"

    file_path = output_path / filename
    file_path.write_text(content, encoding="utf-8")

    return str(file_path)
