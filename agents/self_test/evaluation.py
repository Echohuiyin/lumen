"""评估 Agent：对比分析结果与预期故障，发现差距。

接收 Maintenance Workflow 的分析结果，与已注入的故障特征进行对比，
评估分析的准确性，发现不足之处。
"""

from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm_display import call_llm_with_display
from config import get_llm_with_config, load_prompt_from_file


def evaluation_agent_node(state: dict) -> dict:
    """评估节点：对比分析结果与预期故障。

    输入：
    - expected_fault: 预期故障特征（从 fault_generator 输出）
    - kernel_analysis: 内核专家的分析结果
    - expert_results: 各工具专家的分析结果
    - fault_description: 故障描述

    输出：
    - evaluation_score: 评分（0-100）
    - evaluation_details: 详细评估报告
    - gaps_found: 发现的差距列表
    """
    config = state.get("config", {})
    agent_config = config.get("agents", {}).get("evaluation", {})
    default_config = config.get("default", {})

    llm = get_llm_with_config(agent_config, default_config=default_config, agent_name="evaluation")

    # 构建 prompt
    expected_fault = state.get("expected_fault", {})
    kernel_analysis = state.get("kernel_analysis", "")
    expert_results = state.get("expert_results", [])
    fault_description = state.get("fault_description", "")

    # 汇总专家结果
    expert_summaries = []
    for result in expert_results:
        expert_summaries.append(
            f"### {result['expert_name']}（{result['expert_type']}）\n{result['analysis_output']}"
        )

    user_content = f"""## 预期故障（已知真相）

故障类型: {fault_description}
预期 Panic 模式: {expected_fault.get('expected_panic', 'N/A')}
预期根因: {expected_fault.get('expected_root_cause', 'N/A')}
难度级别: {expected_fault.get('difficulty', 'N/A')}

## 实际分析结果

### 内核专家分析
{kernel_analysis}

### 工具专家分析
{chr(10).join(expert_summaries) if expert_summaries else '无工具专家分析结果'}

## 评估任务

请对比分析结果与预期故障，进行以下评估：

1. **根因定位准确性**：分析是否正确识别了故障的根本原因？
2. **Panic 模式识别**：分析是否正确识别了 panic 的触发模式？
3. **分析路径完整性**：从输入到根因的分析路径是否完整？
4. **复现用例正确性**：复现用例是否能真正触发预期故障？

输出格式：

EVALUATION_SCORE: <0-100分>

ROOT_CAUSE_MATCH: <是/否/部分>
ROOT_CAUSE_DETAILS: <详细说明>

PANIC_MATCH: <是/否/部分>
PANIC_DETAILS: <详细说明>

GAPS_FOUND:
- <差距1>
- <差距2>
...

IMPROVEMENT_SUGGESTIONS:
- <改进建议1>
- <改进建议2>
...
"""

    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", "prompts/self_test/evaluation.md")
    )

    response = call_llm_with_display(
        "评估专家", "对比分析", llm,
        [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
    )

    text = response.content.strip()

    # 解析评估结果
    evaluation_score = _extract_score(text)
    gaps_found = _extract_gaps(text)
    improvement_suggestions = _extract_improvements(text)

    return {
        "evaluation_score": evaluation_score,
        "evaluation_details": text,
        "gaps_found": gaps_found,
        "improvement_suggestions": improvement_suggestions,
    }


def _extract_score(text: str) -> int:
    """从评估文本中提取分数。"""
    import re
    match = re.search(r"EVALUATION_SCORE:\s*(\d+)", text)
    if match:
        return int(match.group(1))
    return 0


def _extract_gaps(text: str) -> list[str]:
    """从评估文本中提取发现的差距。"""
    import re
    match = re.search(r"GAPS_FOUND:\s*\n((?:- .+\n?)+)", text)
    if match:
        gaps_text = match.group(1)
        return [line.strip("- ").strip() for line in gaps_text.strip().split("\n") if line.strip()]
    return []


def _extract_improvements(text: str) -> list[str]:
    """从评估文本中提取改进建议。"""
    import re
    match = re.search(r"IMPROVEMENT_SUGGESTIONS:\s*\n((?:- .+\n?)+)", text)
    if match:
        improvements_text = match.group(1)
        return [line.strip("- ").strip() for line in improvements_text.strip().split("\n") if line.strip()]
    return []