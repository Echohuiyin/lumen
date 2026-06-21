"""自迭代验证工作流状态定义。

扩展 MaintenanceWorkflowState，添加自迭代验证所需的状态字段。
"""

import operator
from typing import Annotated

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class SelfTestIterationState(TypedDict):
    """自迭代验证工作流状态。

    包含故障生成、分析评估、改进迭代所需的完整状态。
    """
    # 基础配置
    messages: Annotated[list, add_messages]
    config_path: str
    config: dict

    # 故障生成
    fault_type: str                     # nullptr/softlockup/deadlock/panic/stack_overflow
    iteration_count: int                # 当前迭代次数
    max_iterations: int                 # 最大迭代次数
    execution_mode: str                 # mock 或 real

    # 生成的测试数据
    generated_vmcore: str               # vmcore 文件路径
    generated_boot_log: str             # boot.log 文件路径
    expected_fault: dict                # 预期故障特征
    fault_description: str              # 故障描述
    fault_generation_error: str         # 故障生成错误信息

    # 传递给 Maintenance Workflow 的输入
    user_input: str                     # 由故障数据构建的输入

    # Maintenance Workflow 的分析结果
    validation_passed: bool
    validation_feedback: str
    required_experts: list[str]
    issue_id: str
    issue_url: str
    expert_type: str
    expert_results: Annotated[list, operator.add]
    reproduce_case: str
    kernel_diagnosis: str
    kernel_analysis: str
    test_result: str
    test_passed: bool
    test_attempts: int

    # 评估结果
    evaluation_score: int               # 评分 (0-100)
    evaluation_details: str             # 详细评估报告
    gaps_found: list[str]               # 发现的差距
    improvement_suggestions: list[str]  # 改进建议（从评估提取）

    # 改进结果
    improvement_report: str             # 改进报告文件路径
    actionable_changes: list[dict]      # 可执行的改进列表
    applied_changes: list[dict]         # 已应用的改进列表
    should_continue: bool               # 是否继续迭代

    # 知识库更新结果
    knowledge_file: str                 # 知识库文档路径
    knowledge_title: str                # 知识库文档标题
    knowledge_import_success: bool      # 是否成功导入 Chroma

    # 最终输出
    final_response: str


def make_self_test_initial_state(
    fault_type: str = "nullptr",
    max_iterations: int = 5,
    config_path: str = "config.json",
    execution_mode: str = "mock",
) -> dict:
    """创建自迭代验证的初始状态。

    Args:
        fault_type: 要注入的故障类型
        max_iterations: 最大迭代次数
        config_path: 配置文件路径
        execution_mode: 执行模式 ("mock" 模拟数据 / "real" 真实故障注入)

    Returns:
        初始状态字典
    """
    return {
        "messages": [],
        "config_path": config_path,
        "config": {},
        "fault_type": fault_type,
        "iteration_count": 0,
        "max_iterations": max_iterations,
        "execution_mode": execution_mode,

        "generated_vmcore": "",
        "generated_boot_log": "",
        "expected_fault": {},
        "fault_description": "",
        "fault_generation_error": "",

        "user_input": "",

        "validation_passed": False,
        "validation_feedback": "",
        "required_experts": [],
        "issue_id": "",
        "issue_url": "",
        "expert_type": "",
        "expert_results": [],
        "reproduce_case": "",
        "kernel_diagnosis": "",
        "kernel_analysis": "",
        "test_result": "",
        "test_passed": False,
        "test_attempts": 0,

        "evaluation_score": 0,
        "evaluation_details": "",
        "gaps_found": [],
        "improvement_suggestions": [],

        "improvement_report": "",
        "actionable_changes": [],
        "applied_changes": [],
        "should_continue": True,

        "knowledge_file": "",
        "knowledge_title": "",
        "knowledge_import_success": False,

        "final_response": "",
    }
