from langgraph.graph import END, START, StateGraph

from agents.kernel_expert import kernel_expert_node
from agents.knowledge_base import knowledge_base_node
from agents.pm import pm_node
from agents.test_expert import test_expert_node
from agents.tool_expert import tool_expert_node
from agents.validator import validator_node
from graph.rn_router import route_after_kernel, route_after_pm, route_after_test, route_after_validator
from graph.rn_state import MaintenanceWorkflowState


def build_maintenance_workflow(*, checkpointer=None):
    """构建维护接口人工作流图。

    START → validator → pm → [tool_expert_1, ..., tool_expert_N] (fan-out)
           → kernel_expert ⇄ test_expert → knowledge_base → END

    - validator 校验输入，不通过直接结束
    - PM 分类并创建 issue，fan-out 到工具专家
    - 工具专家并行分析，结果汇总到内核专家
    - 内核专家构造用例，测试专家验证
    - 测试通过 → 知识库生成；失败 → 回到内核专家重新分析
    """
    builder = StateGraph(MaintenanceWorkflowState)

    builder.add_node("validator", validator_node)
    builder.add_node("pm", pm_node)
    builder.add_node("tool_expert", tool_expert_node)
    builder.add_node("kernel_expert", kernel_expert_node)
    builder.add_node("test_expert", test_expert_node)
    builder.add_node("knowledge_base", knowledge_base_node)

    builder.add_edge(START, "validator")
    builder.add_conditional_edges("validator", route_after_validator)
    builder.add_conditional_edges("pm", route_after_pm)
    builder.add_edge("tool_expert", "kernel_expert")
    builder.add_conditional_edges("kernel_expert", route_after_kernel)
    builder.add_conditional_edges("test_expert", route_after_test)
    builder.add_edge("knowledge_base", END)

    if checkpointer is not None:
        return builder.compile(checkpointer=checkpointer)
    return builder.compile()


# LangGraph Studio / `langgraph dev` entry point
maintenance_graph = build_maintenance_workflow()
