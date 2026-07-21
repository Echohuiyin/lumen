import operator
from typing import Annotated, Literal

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class ToolExpertResult(TypedDict):
    expert_type: str           # "knowledge_search" | "lock_analysis" | "crash_analysis" | "kernel_log_analysis"
    expert_name: str
    analysis_output: str
    structured_output: dict    # 结构化专家输出（状态、证据、artifact、错误）


class TestResult(TypedDict):
    reproduced: bool
    test_output: str
    attempt: int


class MaintenanceWorkflowState(TypedDict):
    messages: Annotated[list, add_messages]
    # 配置
    config_path: str
    config: dict
    # 会话
    session_id: str
    session_dir: str
    # 用户输入
    user_input: str
    execution_mode: str               # 固定为 "real"，实际执行测试
    # Validator 输出
    validation_passed: bool
    validation_feedback: str
    validation_contract: dict          # 结构化输入校验结果
    input_artifacts_contract: dict     # 从用户输入确定性解析出的路径、架构、日志片段
    # PM 输出
    required_experts: list[str]       # 需要调用的专家类型列表
    pm_routing_reason: str            # PM 规则化分类原因
    issue_id: str                     # 创建的 issue ID（打桩）
    issue_url: str                    # issue URL（打桩）
    # Fan-out 传参（Send 设置）
    expert_type: str
    # 工具专家输出（operator.add 累积）
    expert_results: Annotated[list[ToolExpertResult], operator.add]
    # 内核专家输出
    reproduce_case: str               # 构造的必现用例
    kernel_diagnosis: str             # 内核维测方案
    kernel_analysis: str              # 完整分析内容
    all_possible_paths: list[str]      # UAF/refcount 全部候选路径
    max_likely_path: str               # 最大可能路径
    uaf_analysis_contract: dict        # P1 结构化 UAF/refcount 路径事实源
    semcode_path_analysis: dict        # P2 semcode 事件图、覆盖边界或明确 blocked 原因
    kernel_ready_for_test: bool        # 内核专家是否产出了可由 loop 内 SSH-QEMU runner 验证的内容
    kernel_contract: dict              # 结构化内核专家输出（PoC 执行契约）
    target_arch: str                   # QEMU 目标架构：x86_64/arm64/arm32
    boot_kernel_path: str              # QEMU 可启动内核镜像路径（bzImage/Image）
    reproducer_dir: str                # 复现用例目录
    reproducer_module_path: str        # 编译出的 .ko 路径
    expected_signal: str               # 期望在 boot log 中观察到的复现信号
    # 同一内核专家 loop 的确定性 SSH-QEMU 输出
    test_result: str                  # 测试结果详情
    test_passed: bool                 # 是否成功复现
    test_attempts: int                # 测试尝试次数
    test_rounds: list[dict]           # 每轮持久 SSH-QEMU 的确定性结果（按轮次保留）
    test_contract: dict               # 结构化测试结果（状态码、步骤、artifact）
    # 知识库生成输出
    knowledge_file: str               # 知识库文件路径
    final_response: str


def make_initial_state(
    user_input: str = "",
    config_path: str = "config.json",
    session_id: str = "",
    session_dir: str = "",
) -> dict:
    """Create a MaintenanceWorkflowState dict with sensible defaults.

    Args:
        user_input: 用户输入的问题描述
        config_path: 配置文件路径
        session_id: 会话 ID
        session_dir: 会话目录路径
    """
    return {
        "messages": [],
        "config_path": config_path,
        "config": {},
        "session_id": session_id,
        "session_dir": session_dir,
        "user_input": user_input,
        "execution_mode": "real",  # 固定为实际执行模式
        "validation_passed": False,
        "validation_feedback": "",
        "validation_contract": {},
        "input_artifacts_contract": {},
        "required_experts": [],
        "pm_routing_reason": "",
        "issue_id": "",
        "issue_url": "",
        "expert_type": "",
        "expert_results": [],
        "reproduce_case": "",
        "kernel_diagnosis": "",
        "kernel_analysis": "",
        "all_possible_paths": [],
        "max_likely_path": "",
        "uaf_analysis_contract": {},
        "semcode_path_analysis": {},
        "kernel_ready_for_test": True,
        "kernel_contract": {},
        "target_arch": "",
        "boot_kernel_path": "",
        "reproducer_dir": "",
        "reproducer_module_path": "",
        "expected_signal": "",
        "test_result": "",
        "test_passed": False,
        "test_attempts": 0,
        "test_rounds": [],
        "test_contract": {},
        "knowledge_file": "",
        "final_response": "",
    }
