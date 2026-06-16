"""自迭代验证模块。

提供故障注入、评估对比、改进建议的自闭环验证能力。
"""

from agents.self_test.fault_generator import fault_generator_node, get_fault_types_for_testing, get_fault_info
from agents.self_test.evaluation import evaluation_agent_node
from agents.self_test.improvement import improvement_agent_node, rollback_last_prompt_changes
from agents.self_test.knowledge_integration import auto_knowledge_pipeline, generate_knowledge_doc_from_iteration
from agents.self_test.self_test_workflow import SelfTestIterationState, make_self_test_initial_state
from agents.self_test.workflow import build_self_test_workflow, self_test_graph

__all__ = [
    "fault_generator_node",
    "get_fault_types_for_testing",
    "get_fault_info",
    "evaluation_agent_node",
    "improvement_agent_node",
    "rollback_last_prompt_changes",
    "auto_knowledge_pipeline",
    "generate_knowledge_doc_from_iteration",
    "SelfTestIterationState",
    "make_self_test_initial_state",
    "build_self_test_workflow",
    "self_test_graph",
]